"""后台通信线程（与协议无关，经 Driver 翻译）。

约定（AGENTS.md）：
- 「发送」开关 + 频率：电机输出中发控制帧（电流/扭矩或 MIT），否则发驱动的 idle 帧（达妙/HM 为 0xCC）。
  点使能会自动打开发送。
- 「输出中」：有使能命令的协议以电机反馈为准；大疆等无使能命令的协议为软件开关。
- 急停/超限：零电流并锁存，不失能。
- 达妙/HM：量程必须从电机读取后才允许使能；使能中不发 0x7FF 配置帧。
- 测试/闭环由 Run 对象在本线程内按发送频率计算输出，并在本线程内记录数据。
"""
from __future__ import annotations

import collections
import ctypes
import gc
import logging
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import QThread, Signal

from ..control.safety import SafetyLimits, Supervisor
from ..drivers import dji, dm, gd
from ..drivers import dm_protocol as P
from ..drivers.base import Driver, Feedback
from .bus import BusError, CanBus, Frame

log = logging.getLogger("canmotorkit.worker")

LIMIT_RIDS = (0x15, 0x16, 0x17)
NAN = float("nan")


@dataclass
class Sample:
    t: float
    out: float              # 当时下发的电流/扭矩指令（未输出时 NaN）
    fb: Feedback
    pos_mt: float           # 多圈累计位置
    active: bool            # 电机输出中
    ref: float = NAN        # 测试激励/闭环参考值
    mit: P.MitCommand | None = None
    pid_components: tuple[float, float, float] | None = None


@dataclass
class _Req:
    kind: str               # read / write / save
    rid: int
    typ: str
    frame: bytes
    tries: int = 0
    deadline: float = 0.0


class Worker(QThread):
    samples = Signal(object)            # list[Sample]
    frames = Signal(object)             # list[Frame]
    reg_done = Signal(str, int, object, str)
    gd_done = Signal(str, object, str)
    scan_progress = Signal(str, int, int)   # 阶段, i, n
    scan_done = Signal(object)          # list[dict]
    state = Signal(object)
    message = Signal(str)
    limits_changed = Signal(object, bool)
    run_finished = Signal(object)       # Run

    EMIT_HZ = 30

    def __init__(self, bus: CanBus, driver: Driver | None = None):
        super().__init__()
        self.bus = bus
        self.driver: Driver | None = None
        self.rate_hz = 1000.0
        self.sending = False
        self.mode = "current"           # current / mit
        self.req_timeout = 0.1
        self.req_retries = 2
        self.log_enabled = True
        self.safety = Supervisor()

        self._lock = threading.Lock()
        self._target_out = 0.0
        self._target_mit = P.MitCommand()
        self._last_out = NAN
        self._last_mit: P.MitCommand | None = None
        self._sw_en = False
        self._ops: queue.Queue = queue.Queue()
        self._reqs: collections.deque[_Req] = collections.deque()
        self._pending: _Req | None = None
        self._lim_vals: dict[int, float] = {}
        self._scan: dict | None = None
        self._raw_periodic: dict | None = None
        self._run_obj = None
        self._gc_was = True
        self._running = False
        self._frames: list[Frame] = []
        self._last_err_t = 0.0
        self._trip_reported = False
        self._mit_limited = False
        self._tx_t: float | None = None
        self.latencies: collections.deque[float] = collections.deque(maxlen=5000)
        self.motor_code = -1
        self.last_fb: Feedback | None = None
        self.last_fb_t = 0.0
        self._vel_history: collections.deque[tuple[float, float]] = collections.deque(maxlen=128)
        self._vel_last_pos: float | None = None
        self._vel_unwrapped_pos = 0.0
        self._gd_queue = collections.deque()
        self._gd_pending = None
        self._gd_cooldown = 0.0
        self._gd_control = None
        if driver is not None:
            self._apply_driver(driver)

    # ================= 公开接口（线程安全） =================
    def _post(self, fn, *a):
        self._ops.put((fn, a))

    @property
    def active(self) -> bool:
        """电机是否处于输出状态（达妙/HM 以反馈为准，大疆为软件使能）。"""
        d = self.driver
        if d is None:
            return False
        if d.caps.enable_cmd:
            return self.motor_code == P.CODE_ENABLED
        return self._sw_en

    @property
    def enabled(self) -> bool:
        return self.active

    @property
    def configuration_busy(self) -> bool:
        return bool(self._gd_pending and self._gd_pending["kind"] not in gd.QUERIES
                    or any(kind not in gd.QUERIES for kind, _ in self._gd_queue))

    @property
    def limits_ok(self) -> bool:
        return self.driver is not None and self.driver.ready

    @property
    def lim(self) -> P.Limits:
        d = self.driver
        return getattr(d, "lim", P.DEFAULT_LIMITS)

    @property
    def motor_id(self) -> int:
        return self.driver.motor_id if self.driver else -1

    @property
    def master_id(self) -> int:
        return getattr(self.driver, "master_id", -1)

    def set_target_out(self, v: float):
        with self._lock:
            self._target_out = float(v)

    def zero_output(self):
        """在线程内立即发送零输出帧，并清除当前输出目标。"""
        self._post(self._do_zero_output)

    def set_target_mit(self, cmd: P.MitCommand):
        with self._lock:
            self._target_mit = replace(cmd)

    set_target = set_target_mit

    def get_target(self) -> P.MitCommand:
        with self._lock:
            return replace(self._target_mit)

    def get_target_out(self) -> float:
        with self._lock:
            return self._target_out

    def set_mode(self, mode: str):
        if isinstance(self.driver, gd.GdDriver) and mode != self.mode:
            # 在请求切换时清零，不能在队列稍后执行时覆盖用户刚输入的新目标。
            with self._lock:
                self._target_out = 0.0
                self._target_mit = P.MitCommand()
        self._post(self._do_set_mode, mode)

    def gd_request(self, kind: str, value=None):
        self._post(self._do_gd_request, kind, value)

    def set_gd_control(self, kind: str, values: dict):
        self._post(self._do_gd_control, kind, dict(values))

    def set_driver(self, driver: Driver | None):
        self._post(self._do_set_driver, driver)

    def set_sending(self, on: bool):
        self._post(self._do_set_sending, bool(on))

    def reload_limits(self):
        self._post(self._do_reload_limits)

    def enable(self):
        self._post(self._do_enable)

    def disable(self):
        self._post(self._do_disable)

    def estop(self, reason: str = "用户急停"):
        self._post(self._do_estop, reason)

    def reset_safety(self):
        self._post(self._do_reset_safety)

    def set_safety_limits(self, lim: SafetyLimits):
        self._post(setattr, self.safety, "limits", lim)

    def zero_safety_position(self):
        self._post(self.safety.zero_position)

    def clear_error(self):
        self._post(self._do_frames, "clear_frames", "已发送清错")

    def set_zero(self):
        self._post(self._do_zero)

    def read_reg(self, rid: int):
        self._post(self._do_read, rid)

    def write_reg(self, rid: int, value):
        self._post(self._do_write, rid, value)

    def save(self):
        self._post(self._do_save)

    def start_scan(self, dm_ids=range(1, 128), window: float = 0.02, listen: float = 0.3,
                   identify: bool = True):
        self._post(self._do_scan, list(dm_ids), window, listen, identify)

    def stop_scan(self):
        self._post(self._do_stop_scan)

    def send_raw(self, can_id: int, data: bytes, extended: bool = False,
                 rtr: bool = False, dlc: int | None = None):
        self._post(self._send, can_id, bytes(data), extended, rtr, dlc)

    def start_raw_periodic(self, can_id: int, data: bytes, frequency: float,
                           extended: bool = False, rtr: bool = False,
                           dlc: int | None = None):
        """在线程内按指定频率发送原始帧，不经过电机安全限制。"""
        self._post(self._do_start_raw_periodic, can_id, bytes(data), float(frequency),
                   extended, rtr, dlc)

    def stop_raw_periodic(self):
        self._post(self._do_stop_raw_periodic)

    def start_run(self, run):
        self._post(self._do_start_run, run)

    def stop_run(self):
        self._post(self._abort_run, "用户停止")

    def stop(self):
        self._post(self._do_shutdown)

    # ================= 内部 =================
    def _msg(self, s: str):
        log.info(s)
        self.message.emit(s)

    def _err(self, s: str):
        log.warning(s)
        now = time.perf_counter()
        if now - self._last_err_t > 1.0:
            self._last_err_t = now
            self.message.emit("错误: " + s)

    def _send(self, can_id: int, data: bytes, extended: bool = False,
              rtr: bool = False, dlc: int | None = None) -> bool:
        try:
            self.bus.send(can_id, data, extended=extended, rtr=rtr, dlc=dlc)
        except BusError as e:
            self._err(str(e))
            return False
        if self.log_enabled:
            self._frames.append(Frame(time.perf_counter(), can_id, bytes(data), True,
                                      extended, rtr, dlc))
        return True

    def _send_all(self, frames) -> bool:
        ok = True
        for cid, d in frames:
            ok = self._send(cid, d, extended=bool(self.driver and self.driver.extended)) and ok
        return ok

    def _is_dm(self) -> bool:
        return isinstance(self.driver, dm.DmDriver)

    def _config_blocked(self) -> bool:
        return self._is_dm() and self.motor_code == P.CODE_ENABLED

    BLOCK_TIP = "电机使能中，请先失能"

    # ---------- 电机选择 ----------
    def _apply_driver(self, driver: Driver | None):
        self.driver = driver
        self.motor_code = -1
        self.last_fb = None
        self._sw_en = False
        self._gd_queue.clear()
        self._gd_pending = None
        self._gd_control = None
        with self._lock:
            self._target_out = 0.0
            self._target_mit = P.MitCommand()
        self._last_out = NAN
        self._reqs.clear()
        self._pending = None
        self.latencies.clear()
        self._vel_history.clear()
        self._vel_last_pos = None
        self._vel_unwrapped_pos = 0.0
        self.safety.mt.reset()
        if driver is None:
            return
        self.safety.set_wrap_span(driver.wrap_span)
        if not driver.caps.mit:
            self.mode = "current"
        if self._is_dm() and driver.caps.registers:
            driver.invalidate_limits()
            self._queue_limit_reads()
        if driver.protocol == "gd":
            self.mode = "current"
            for kind in gd.QUERIES:
                self._gd_queue.append((kind, None))

    def _do_set_driver(self, driver: Driver | None):
        if self.configuration_busy:
            self._msg("GD 设置正在执行，请等待操作结束再切换电机")
            return
        if self.active:
            self._msg("请先失能当前电机再切换")
            return
        self._do_stop_raw_periodic()
        self._abort_run("切换电机")
        self._apply_driver(driver)
        self.limits_changed.emit(self.lim, self.limits_ok)
        self._msg("已选择电机：" + (driver.title() if driver else "无"))

    def _do_set_mode(self, mode: str):
        if mode not in ("current", "mit", "gd") or (mode == "gd" and not isinstance(self.driver, gd.GdDriver)):
            self._msg("当前电机不支持该控制模式")
            return
        if mode == "mit" and not (self.driver and self.driver.caps.mit):
            self._msg("当前电机不支持 MIT")
            return
        if mode != self.mode:
            self._abort_run("切换控制模式")
            if isinstance(self.driver, gd.GdDriver):
                self._gd_control = None
                if self.active:
                    self._send_all(self.driver.stop_frames())
            self.mode = mode

    def _do_set_sending(self, on: bool):
        if not on and self.sending and self.active and self.driver is not None:
            # 达妙/HM 未设 can_timeout 时会一直保持最后一帧指令，停发前先下发零输出。
            self._send_all(self.driver.stop_frames())
            self._last_out = NAN
            self._msg("发送已关闭：已先下发零输出，电机仍处于使能状态")
        self.sending = on

    # ---------- 量程 ----------
    def _queue_limit_reads(self):
        self._lim_vals = {}
        self._reqs = collections.deque(r for r in self._reqs if r.rid not in LIMIT_RIDS)
        for rid in LIMIT_RIDS:
            self._reqs.append(_Req("read", rid, "f", P.read_reg_frame(self.motor_id, rid)))

    def _do_reload_limits(self):
        if not self._is_dm() or not self.driver.caps.registers:
            if self._is_dm():
                self._msg("当前型号不支持通过 CAN 读取量程，使用默认量程")
            return
        if self._config_blocked():
            self._msg("不能重读量程：" + self.BLOCK_TIP)
            return
        self.driver.invalidate_limits()
        self._queue_limit_reads()
        self.limits_changed.emit(self.lim, False)

    def _on_limit_value(self, rid: int, val):
        v = float(val)
        if not math.isfinite(v) or v <= 0:
            self._msg(f"量程寄存器 0x{rid:02X} 值无效: {v}")
            return
        self._lim_vals[rid] = v
        if all(r in self._lim_vals for r in LIMIT_RIDS):
            was = self.driver.ready
            self.driver.set_limits(self._lim_vals[0x15], self._lim_vals[0x16], self._lim_vals[0x17])
            self.safety.set_wrap_span(self.driver.wrap_span)
            self.limits_changed.emit(self.lim, True)
            if not was:
                L = self.lim
                self._msg(f"已读取量程: p±{L.p_max:.7g} v±{L.v_max:.7g} t±{L.t_max:.7g}")

    # ---------- 使能 ----------
    def _do_enable(self):
        d = self.driver
        if d is None:
            self._msg("未选择电机")
            return
        if self._scan is not None:
            self._msg("扫描中，不能使能")
            return
        if self._gd_pending is not None or self._gd_queue:
            self._msg("GD 查询/设置尚未结束，请稍后开始输出")
            return
        if not d.ready:
            self._msg("尚未从电机读到量程，禁止使能（可在参数页重读）")
            return
        if not self.sending:
            self.sending = True
            self._msg("已自动打开发送")
        if d.caps.enable_cmd:
            if self._send_all(d.enable_frames()):
                self._msg("已发送使能")
        else:
            self._sw_en = True
            # 软件使能的协议（大疆/GD）即使一帧反馈都没收到，开始输出后也必须计时保护。
            self.safety.arm_watchdog(time.perf_counter())
            self._msg("已使能（软件开关，开始发送电流指令）")

    def _do_disable(self):
        d = self.driver
        if d is None:
            return
        self._abort_run("失能")
        self._gd_control = None
        with self._lock:
            self._target_out = 0.0
            self._target_mit = P.MitCommand()
        if d.caps.enable_cmd:
            # 先撤掉扭矩/电流指令，再发送失能，避免保留上一条输出。
            self._send_all(d.stop_frames())
            if self._send_all(d.disable_frames()) and self.motor_code == P.CODE_ENABLED:
                self.motor_code = P.CODE_DISABLED      # 立即改发 idle 帧，以后续反馈为准
            self._msg("已发送失能")
        else:
            self._sw_en = False
            self._send_all(d.stop_frames())
            self._send_all(d.disable_frames())
            self._msg("已失能（发送零电流）")

    def _do_zero_output(self):
        with self._lock:
            self._target_out = 0.0
            self._target_mit = P.MitCommand()
        self._gd_control = None
        d = self.driver
        if d is not None and self.active:
            self._send_all(d.stop_frames())

    def _do_estop(self, reason: str):
        self._abort_run(reason)
        self.safety.trip(reason)
        if self.driver is not None:
            self._send_all(self.driver.stop_frames())
        self._msg(f"急停（{reason}）：已发送零电流并锁存，电机未失能")

    def _do_reset_safety(self):
        if self.safety.reset():
            self._trip_reported = False
            self._msg("已解除急停/超限锁存")
        else:
            self._msg("不能解除：" + self.safety.reason)

    def _do_frames(self, name: str, text: str):
        if self.driver is not None:
            fr = getattr(self.driver, name)()
            if fr and self._send_all(fr):
                self._msg(text)

    def _do_zero(self):
        if isinstance(self.driver, gd.GdDriver):
            self._do_gd_request("zero")
            return
        if self._config_blocked() or (self.driver and self.driver.protocol == "gd" and self.active):
            self._msg("不能设零点：" + self.BLOCK_TIP)
            return
        self._do_frames("zero_frames", "已发送设零点")
        self.safety.mt.reset()

    def _do_shutdown(self):
        self._do_stop_raw_periodic()
        self._abort_run("退出")
        if self.driver is not None and self.active:
            self._send_all(self.driver.stop_frames())
            self._send_all(self.driver.disable_frames())
        self._running = False

    def _on_trip(self):
        self._gd_control = None
        if isinstance(self.driver, gd.GdDriver):
            with self._lock:
                self._target_out = 0.0
                self._target_mit = P.MitCommand()
        if not self._trip_reported:
            self._trip_reported = True
            self._abort_run("安全限制")
            self._msg("安全限制触发零电流：" + self.safety.reason)

    # ---------- 测试 / 闭环 ----------
    def _do_start_run(self, run):
        why = ""
        if not self.active:
            why = "请先使能电机"
        elif self.safety.tripped:
            why = "安全锁存中，请先解除"
        if why:
            self._msg(why)
            run.finish(why)
            self.run_finished.emit(run)
            return
        self._abort_run("启动新测试")
        self._gd_control = None
        fb = self.last_fb
        run.start(time.perf_counter(), self.safety.pos, fb.vel if fb else 0.0)
        self._run_obj = run
        self._gc_was = gc.isenabled()
        gc.disable()
        self._msg(f"开始：{run.title}")

    def _finish_run(self, run, reason: str = ""):
        if self._run_obj is not run:
            return
        self._run_obj = None
        if self._gc_was:
            gc.enable()
        run.finish(reason)
        if self.driver is not None and self.active:
            self._send_all(self.driver.stop_frames())
            self.set_target_out(0.0)
        self._msg(f"结束：{run.title}" + (f"（{reason}）" if reason else "") + "，输出已归零")
        self.run_finished.emit(run)

    def _abort_run(self, reason: str):
        if self._run_obj is not None:
            self._finish_run(self._run_obj, reason)

    # ---------- 原始帧周期发送 ----------
    def _do_start_raw_periodic(self, can_id: int, data: bytes, frequency: float,
                               extended: bool = False, rtr: bool = False,
                               dlc: int | None = None):
        if frequency <= 0.0:
            self._do_stop_raw_periodic()
            return
        self._raw_periodic = {
            "can_id": int(can_id),
            "data": bytes(data),
            "period": 1.0 / frequency,
            "next": time.perf_counter(),
            "extended": bool(extended),
            "rtr": bool(rtr),
            "dlc": dlc,
        }

    def _do_stop_raw_periodic(self):
        self._raw_periodic = None

    def _service_raw_periodic(self, now: float):
        task = self._raw_periodic
        if task is None or now < task["next"]:
            return
        task["next"] = now + task["period"]
        self._send(task["can_id"], task["data"], task["extended"],
                   task["rtr"], task["dlc"])

    # ---------- 配置 ----------
    def _do_read(self, rid: int):
        if not self._is_dm() or not self.driver.caps.registers:
            return
        if self._config_blocked():
            self.reg_done.emit("read", rid, None, self.BLOCK_TIP)
            return
        reg = P.REG_BY_RID.get(rid)
        if any(r.kind == "read" and r.rid == rid for r in self._reqs):
            return
        self._reqs.append(_Req("read", rid, reg.typ if reg else "u",
                               P.read_reg_frame(self.motor_id, rid)))

    def _do_write(self, rid: int, value):
        if not self._is_dm() or not self.driver.caps.registers:
            return
        reg = P.REG_BY_RID.get(rid)
        typ = reg.typ if reg else "u"
        if reg and reg.ro:
            self.reg_done.emit("write", rid, None, "本软件中为只读寄存器")
            return
        if self._config_blocked():
            self.reg_done.emit("write", rid, None, self.BLOCK_TIP)
            return
        try:
            frame = P.write_reg_frame(self.motor_id, rid, typ, value)
        except Exception as e:
            self.reg_done.emit("write", rid, None, f"数值无效: {e}")
            return
        self._reqs.append(_Req("write", rid, typ, frame))

    def _do_save(self):
        if not self._is_dm() or not self.driver.caps.registers:
            return
        if self._config_blocked():
            self.reg_done.emit("save", 0, None, "保存参数只在失能模式下生效：" + self.BLOCK_TIP)
            return
        self._reqs.append(_Req("save", 0, "u", P.save_frame(self.motor_id)))

    def _finish_req(self, d: bytes):
        p = self._pending
        self._pending = None
        r = P.parse_config_reply(d)
        val = True if p.kind == "save" else P.decode_value(p.typ, r.raw4)
        if p.kind == "read" and p.rid in LIMIT_RIDS:
            self._on_limit_value(p.rid, val)
        self.reg_done.emit(p.kind, p.rid, val, "")

    def _service_config(self, now: float):
        p = self._pending
        if p is not None:
            if now >= p.deadline:
                if p.tries <= self.req_retries:
                    p.tries += 1
                    p.deadline = now + self.req_timeout
                    self._send(P.CONFIG_ID, p.frame)
                else:
                    self._pending = None
                    if p.kind == "read" and p.rid in LIMIT_RIDS:
                        self._msg(f"量程寄存器 0x{p.rid:02X} 读取超时，禁止使能（可在参数页重读）")
                    self.reg_done.emit(p.kind, p.rid, None, "超时无应答")
            return
        if self._reqs and self._scan is None and not self._config_blocked():
            p = self._reqs.popleft()
            p.tries = 1
            p.deadline = now + self.req_timeout
            self._pending = p
            self._send(P.CONFIG_ID, p.frame)

    # ---------- 高擎 GD 查询与系统命令 ----------
    def _do_gd_request(self, kind, value=None):
        if not isinstance(self.driver, gd.GdDriver):
            return
        if self.active or self._scan is not None:
            self.gd_done.emit(kind, None, "请先停止输出并结束扫描")
            return
        try:
            if kind in gd.QUERIES:
                self.driver.query_frame(kind)
            else:
                self.driver.system_frame(kind, value)
                if self._gd_pending is not None or self._gd_queue:
                    raise ValueError("GD 正在处理请求，请稍后再试")
        except (KeyError, ValueError) as e:
            self.gd_done.emit(kind, None, str(e))
            return
        if (kind, value) not in self._gd_queue:
            self._gd_queue.append((kind, value))

    def _finish_gd(self, value=None, error=""):
        p = self._gd_pending
        self._gd_pending = None
        if p is None:
            return
        kind = p["kind"]
        if error:
            self._gd_cooldown = time.perf_counter() + 0.2
        elif kind in gd.QUERIES:
            self.driver.apply_identity(kind, value)
        elif kind == "change_id":
            self.driver.motor_id = p["value"]
            self.last_fb = None
            self.last_fb_t = 0.0
            self._gd_control = None
            self.safety.mt.reset()
        elif kind == "zero":
            self.safety.mt.reset()
        self.gd_done.emit(kind, value, error)

    def _gd_send_phase(self, p, phase):
        p["phase"] = phase
        p["deadline"] = time.perf_counter() + max(0.1, self.req_timeout)
        d = self.driver
        if phase in ("probe", "verify"):
            cid, data = gd.TX_BASE | p["value"], bytes((0, gd.QUERY_STATUS))
        else:
            cid = d.tx_id
            data = d.query_frame(p["kind"]) if p["kind"] in gd.QUERIES else d.system_frame(p["kind"], p["value"])
        if not self._send(cid, data, extended=True):
            self._finish_gd(error="发送失败；未确认电机是否执行")

    def _service_gd(self, now):
        if not isinstance(self.driver, gd.GdDriver) or self.active or self._scan is not None:
            return
        p = self._gd_pending
        if p is not None:
            if now < p["deadline"]:
                return
            if p["phase"] == "probe":
                self._gd_send_phase(p, "command")
            elif p["kind"] == "change_id" and p["phase"] == "command":
                self._gd_send_phase(p, "verify")
            elif p["kind"] == "reboot":
                self._finish_gd({"sent": True, "confirmed": False})  # 文档明确软重启无返回
            else:
                self._finish_gd(error="超时无完整应答；未自动重发，请核对电机状态")
            return
        if not self._gd_queue or now < self._gd_cooldown:
            return
        kind, value = self._gd_queue.popleft()
        p = self._gd_pending = {"kind": kind, "value": value, "old_id": self.driver.motor_id}
        if kind == "change_id" and value == self.driver.motor_id:
            self._finish_gd(value)
            return
        self._gd_send_phase(p, "probe" if kind == "change_id" else "command")
        if kind == "periodic" and self._gd_pending is not None:
            # 文档未定义周期设置应答，明确只报告已发送，不伪造设置成功。
            self._finish_gd({"period_us": value, "confirmed": False})

    def _gd_rx(self, f):
        p = self._gd_pending
        if p is None or not f.extended or f.rtr:
            return
        kind, phase = p["kind"], p["phase"]
        d = self.driver
        if phase in ("probe", "verify"):
            if gd.motor_from_status(f.can_id, f.data) == p["value"]:
                if phase == "probe":
                    self._finish_gd(error="目标 ID 已有电机应答，请选择其他 ID")
                else:
                    self._finish_gd(p["value"])
            return
        ids = {d.fb_id}
        if kind == "change_id":
            ids.add(gd.RX_BASE | (p["value"] << 8))
        if f.can_id not in ids or not f.data:
            return
        if kind in gd.QUERIES and f.data[0] == gd.QUERIES[kind]:
            try:
                value = d.parse_identity(kind, f.data)
            except ValueError as e:
                self._finish_gd(error=str(e))
            else:
                self._finish_gd(value)
        elif kind not in gd.QUERIES and len(f.data) == 2 and f.data[0] == 3:
            if f.data[1]:
                self._finish_gd(error=f"电机拒绝操作，结果码 0x{f.data[1]:02X}")
            elif kind == "change_id":
                self._gd_send_phase(p, "verify")
            else:
                self._finish_gd(True)

    def _do_gd_control(self, kind, values):
        d = self.driver
        if not isinstance(d, gd.GdDriver) or not self.active or self.safety.tripped:
            self._msg("请先开始输出并解除安全锁存")
            return
        try:
            d.control_frames(kind, values)
        except (KeyError, ValueError) as e:
            self._msg(str(e))
            return
        if self.mode != "gd":
            self._msg("请先选择 GD 控制页")
            return
        self._gd_control = (kind, values)

    # ---------- 扫描 ----------
    def _do_scan(self, ids: list[int], window: float, listen: float, identify: bool):
        if self._gd_pending is not None or self._gd_queue:
            self._msg("GD 查询/设置尚未结束，请稍后扫描")
            self.scan_done.emit([])
            return
        if self.active:
            self._msg("不能扫描：请先失能")
            self.scan_done.emit([])
            return
        self._abort_run("扫描")
        self._scan = {"phase": "listen", "until": time.perf_counter() + listen, "listen": listen,
                      "dji": {}, "ids": ids, "i": -1, "cur": None, "deadline": 0.0,
                      "window": window, "dm": {}, "identify": identify, "probe": [], "pi": -1,
                      "preq": None, "pres": {}, "gd": {}, "gi": -1, "gd_info": {}, "gmi": -1}
        self.scan_progress.emit("监听大疆反馈", 0, 1)

    def _do_stop_scan(self):
        if self._scan is not None:
            self._finish_scan()

    def _scan_rx(self, f: Frame):
        s = self._scan
        d = f.data
        if f.rtr:
            return
        if f.extended:
            mid = gd.motor_from_status(f.can_id, d)
            if mid is not None:
                s["gd"][mid] = f.can_id
                if s["phase"] == "gd" and mid == s.get("gcur"):
                    s["deadline"] = 0.0
            if s["phase"] == "gd_model" and s.get("gcur") is not None:
                if f.can_id == gd.RX_BASE | (s["gcur"] << 8) and d and d[0] == gd.QUERIES["model"]:
                    try:
                        s["gd_info"][s["gcur"]] = gd.GdDriver.parse_identity("model", d)
                    except ValueError:
                        pass
                    s["deadline"] = 0.0
            return
        if s["phase"] == "listen":
            if 0x201 <= f.can_id <= 0x20B and len(d) >= 7:
                e = s["dji"].setdefault(f.can_id, {"n": 0, "data": d})
                e["n"] += 1
                e["data"] = d
        elif s["phase"] == "dm":
            cur = s["cur"]
            if (cur is not None and cur not in s["dm"] and len(d) == 8 and f.can_id != P.CONFIG_ID
                    and not (0x201 <= f.can_id <= 0x20B) and d[2] not in P.CONFIG_CMDS):
                s["dm"][cur] = (f.can_id, bytes(d))
        elif s["phase"] == "probe" and s["preq"] is not None:
            if P.matches_request(d, s["preq"]):
                s["pres"][s["probe"][s["pi"]]] = True
                s["deadline"] = 0.0

    def _service_scan(self, now: float):
        s = self._scan
        if s is None or self._pending is not None:
            return
        if s["phase"] == "listen":
            if now < s["until"]:
                return
            s["phase"] = "dm"
        if s["phase"] == "dm":
            if now < s["deadline"]:
                return
            s["i"] += 1
            if s["i"] < len(s["ids"]):
                cur = s["ids"][s["i"]]
                s["cur"] = cur
                s["deadline"] = now + s["window"]
                self._send(P.CONFIG_ID, P.query_state_frame(cur))
                if s["i"] % 4 == 0:
                    self.scan_progress.emit("查询达妙/HM", s["i"], len(s["ids"]))
                return
            s["cur"] = None
            s["phase"] = "probe"
            if s["identify"]:
                s["probe"] = [(mid, rid) for mid in sorted(s["dm"]) for rid in (0x0A, 0x3C)]
            s["deadline"] = 0.0
        if s["phase"] == "probe":
            if now < s["deadline"]:
                return
            s["pi"] += 1
            if s["pi"] < len(s["probe"]):
                mid, rid = s["probe"][s["pi"]]
                s["preq"] = P.read_reg_frame(mid, rid)
                s["deadline"] = now + 0.06
                self._send(P.CONFIG_ID, s["preq"])
                self.scan_progress.emit("识别型号", s["pi"], len(s["probe"]))
                return
            s["phase"] = "gd"
            s["deadline"] = 0.0
        if s["phase"] == "gd":
            if now < s["deadline"]:
                return
            s["gi"] += 1
            while s["gi"] < len(s["ids"]):
                mid = s["ids"][s["gi"]]
                if 1 <= mid <= 126 and mid not in s["gd"]:
                    s["gcur"] = mid
                    s["deadline"] = now + s["window"]
                    self._send(gd.TX_BASE | mid, bytes((0x00, gd.QUERY_STATUS)), extended=True)
                    self.scan_progress.emit("查询高擎 GD", s["gi"], len(s["ids"]))
                    return
                s["gi"] += 1
            s["phase"] = "gd_model"
            s["gmodels"] = sorted(s["gd"]) if s["identify"] else []
            s["gcur"] = None
            s["deadline"] = 0.0
        if s["phase"] == "gd_model":
            if now < s["deadline"]:
                return
            s["gmi"] += 1
            if s["gmi"] < len(s["gmodels"]):
                s["gcur"] = s["gmodels"][s["gmi"]]
                s["deadline"] = now + max(0.06, s["window"])
                self._send(gd.TX_BASE | s["gcur"], bytes((0, 7)), extended=True)
                self.scan_progress.emit("识别高擎型号", s["gmi"], len(s["gmodels"]))
                return
            self._finish_scan()

    def _finish_scan(self):
        s = self._scan
        self._scan = None
        found = []
        min_n = max(5, int(s["listen"] * 200))      # 大疆约 1 kHz 广播
        for fb_id, e in sorted(s["dji"].items()):
            cands = dji.guess_models(fb_id, e["data"])
            if e["n"] < min_n or not cands:
                continue
            rate = e["n"] / s["listen"]
            found.append({"protocol": "dji", "model": cands[0][0], "motor_id": cands[0][1],
                          "master_id": None, "fb_id": fb_id, "candidates": cands,
                          "evidence": f"反馈 0x{fb_id:03X} 广播约 {rate:.0f} Hz" +
                          ("；与其它型号 ID 重叠，请确认" if len(cands) > 1 else "")})
        for mid, (cid, d) in sorted(s["dm"].items()):
            has_mode = s["pres"].get((mid, 0x0A), False)
            has_vbus = s["pres"].get((mid, 0x3C), False)
            if not s["identify"]:
                model, ev = "达妙/HM 通用", "0xCC 有应答"
            elif has_mode:
                model, ev = "DM-J8009P", "有 CTRL_MODE(0x0A) → 达妙（型号请确认）"
            elif has_vbus:
                model, ev = "HM9816G9", "有 v_bus(0x3C)、无 CTRL_MODE → HM"
            else:
                model, ev = "达妙/HM 通用", "特征不足"
            found.append({"protocol": "dm", "model": model, "motor_id": mid, "master_id": cid,
                          "fb_id": cid, "candidates": [(model, mid)],
                          "evidence": f"0xCC 应答 ID 0x{cid:X}；{ev}", "code": d[0] >> 4})
        for mid, cid in sorted(s["gd"].items()):
            d = gd.GdDriver(motor_id=mid)
            info = s["gd_info"].get(mid)
            if info:
                d.apply_identity("model", info)
            found.append({"protocol": "gd", "model": d.model, "motor_id": mid,
                          "master_id": None, "fb_id": cid, "candidates": [(d.model, mid)],
                          "evidence": "GD 扩展帧 0x0E 状态应答", "gd_identity": d.identity})
        self.scan_progress.emit("完成", 1, 1)
        self.scan_done.emit(found)

    # ---------- 接收 ----------
    def _estimate_velocity(self, t: float, pos: float, span: float) -> float:
        """从 6623 的单圈角度反馈估算速度。

        6623 不在反馈帧中提供 RPM。PCANBasic 这里使用主机读取时间，
        同一批从接收队列取出的帧可能拥有非常接近的时间戳，不能直接用
        相邻两帧做差分，否则会把正常角度变化放大成数千 rad/s。
        """
        if self._vel_last_pos is None:
            self._vel_unwrapped_pos = pos
        else:
            delta = pos - self._vel_last_pos
            if delta > span / 2:
                delta -= span
            elif delta < -span / 2:
                delta += span
            self._vel_unwrapped_pos += delta
        self._vel_last_pos = pos

        if self._vel_history and t <= self._vel_history[-1][0]:
            return NAN
        self._vel_history.append((t, self._vel_unwrapped_pos))
        while len(self._vel_history) > 2 and t - self._vel_history[0][0] > 0.05:
            self._vel_history.popleft()

        # 至少跨过 4 ms 再估算，避免 PCAN 接收队列批处理造成的极端斜率。
        points = [(pt, pp) for pt, pp in self._vel_history if t - pt >= 0.004]
        if not points:
            return NAN
        t0, _ = points[-1]
        points = [(pt, pp) for pt, pp in points if pt <= t0]
        if not points:
            return NAN
        # 用最近 20 ms 内的线性拟合降低角度量化和单帧时间抖动的影响。
        points = [(pt, pp) for pt, pp in points if t - pt <= 0.02]
        if len(points) < 2:
            return NAN
        mean_t = sum(pt for pt, _ in points) / len(points)
        mean_p = sum(pp for _, pp in points) / len(points)
        den = sum((pt - mean_t) ** 2 for pt, _ in points)
        if den <= 1.0e-12:
            return NAN
        vel = sum((pt - mean_t) * (pp - mean_p) for pt, pp in points) / den
        return vel if math.isfinite(vel) else NAN

    def _on_rx(self, f: Frame) -> Sample | None:
        if f.rtr:
            return None
        self._gd_rx(f)
        d = f.data
        p = self._pending
        if p and not f.extended and self._is_dm() and f.can_id == self.master_id and P.matches_request(d, p.frame):
            self._finish_req(d)
            return None
        if self._scan is not None:
            self._scan_rx(f)
            if self._scan["phase"] != "listen":
                return None
        drv = self.driver
        if drv is None or f.extended != drv.extended or not drv.accepts(f.can_id, d):
            return None
        fb = drv.parse(f.can_id, d)
        if fb is None:
            return None
        if math.isnan(fb.vel):
            fb.vel = self._estimate_velocity(
                f.t, fb.pos, max(float(drv.wrap_span), 1.0e-9))
        if self._tx_t is not None and (drv.caps.enable_cmd or drv.extended):
            self.latencies.append(f.t - self._tx_t)
            self._tx_t = None
        if drv.caps.status_code:
            if fb.code != self.motor_code and self.motor_code != -1 and drv.is_fault(fb.code):
                self._msg(f"电机报告 {drv.code_text(fb.code)}")
            if drv.caps.enable_cmd and fb.enabled is False and self._run_obj is not None:
                self._abort_run("电机已退出使能")
            self.motor_code = fb.code
            if drv.protocol == "gd" and drv.is_fault(fb.code):
                self.safety.trip("电机报告 " + drv.code_text(fb.code))
        self.last_fb = fb
        self.last_fb_t = f.t
        self.safety.on_feedback(f.t, fb.pos, fb.vel, fb.max_temp())
        if self.safety.tripped and self.active:
            self._on_trip()
        elif not self.safety.tripped:
            self._trip_reported = False
        run = self._run_obj
        pid_components = None
        if run is not None:
            run.record(f.t, self._last_out, fb, self.safety.pos)
            get_components = getattr(run, "components", None)
            if get_components is not None:
                pid_components = get_components()
        act = self.active
        return Sample(f.t, self._last_out if act else NAN, fb, self.safety.pos, act,
                      run.ref if run is not None else NAN,
                      self._last_mit if act and self.mode == "mit" else None,
                      pid_components)

    # ---------- 发送 ----------
    def _tick(self, now: float) -> bool:
        d = self.driver
        if d is None:
            return False
        if not self.active:
            self._last_out = NAN
            if self._scan is None and self._pending is None and self._gd_pending is None:
                fr = d.idle_frames()
                if fr and self._send_all(fr):
                    self._tx_t = now
                    return True
            return False
        mit = None
        if isinstance(d, gd.GdDriver):
            # GD 原生/MIT 模式的前馈可能为零但增益仍会出力，不能只按 t 判断。
            self.safety.check_timeout(now, True)
            if self.safety.tripped:
                self._on_trip()
        if self.safety.tripped or not d.ready:
            fr, out = d.stop_frames(), 0.0
        else:
            run = self._run_obj
            if run is not None:
                u = run.output(now, self.last_fb, self.safety.pos)
                if u is None:
                    self._finish_run(run)
                    u = 0.0
                out = self.safety.filter_output(now, u)
                track = getattr(run, "track_output", None)
                if track is not None:
                    actual = out
                    if d.out_max > 0:
                        actual = max(-d.out_max, min(d.out_max, actual))
                    track(actual)
                fr = d.output_frames(out)
            elif self.mode == "gd" and isinstance(d, gd.GdDriver):
                if self._gd_control is None:
                    fr, out = d.stop_frames(), 0.0
                else:
                    kind, values = self._gd_control
                    values = dict(values)
                    sl = self.safety.limits
                    if kind in ("velocity", "position_velocity_torque") and sl.vel_enabled:
                        values["velocity"] = P.clamp(values["velocity"], -sl.vel_max, sl.vel_max)
                    if kind == "position_velocity_torque" and sl.out_enabled:
                        values["torque"] = min(values["torque"], sl.out_max)
                    if kind in ("velocity", "position") and sl.out_enabled:
                        self._msg("该原生模式不带扭矩上限；请选择位置+速度+力矩模式或调整左侧限幅设置")
                        self._gd_control = None
                        fr, out = d.stop_frames(), 0.0
                    else:
                        fr, out = d.control_frames(kind, values), NAN
            elif self.mode == "mit" and d.caps.mit:
                cmd = self.get_target()
                fb = self.last_fb
                p, v, kp, kd, t, limited = self.safety.filter_mit(
                    now, cmd.p, cmd.v, cmd.kp, cmd.kd, cmd.t,
                    fb.pos if fb is not None else None, fb.vel if fb is not None else None)
                if limited and not self._mit_limited:
                    self._msg("MIT 等效扭矩 kp·Δp+kd·Δv+t 超出左侧限幅，已自动收缩位置目标/前馈")
                self._mit_limited = limited
                L = self.lim
                mit = P.MitCommand(P.clamp(p, L.p_min, L.p_max), P.clamp(v, L.v_min, L.v_max),
                                   P.clamp(kp, L.kp_min, L.kp_max),
                                   P.clamp(kd, L.kd_min, L.kd_max), P.clamp(t, L.t_min, L.t_max))
                out = mit.t
                fr = d.mit_frames(mit)
            else:
                out = self.safety.filter_output(now, self.get_target_out())
                fr = d.output_frames(out)
            if self.safety.tripped:
                self._on_trip()
                fr, out, mit = d.stop_frames(), 0.0, None
        if self._send_all(fr):
            self._last_out = out
            self._last_mit = mit
            self._tx_t = now
            return True
        return False

    def latency_stats(self) -> dict:
        xs = sorted(self.latencies)
        if not xs:
            return {}
        n = len(xs)
        return {"n": n, "p50": xs[n // 2], "p95": xs[min(n - 1, int(n * 0.95))], "max": xs[-1]}

    # ---------- 主循环 ----------
    def run(self):
        winmm = None
        if sys.platform == "win32":
            try:
                winmm = ctypes.windll.winmm
                winmm.timeBeginPeriod(1)
            except Exception:
                winmm = None
        self._running = True
        now = time.perf_counter()
        next_tx = next_emit = now
        next_bus_check = now + 1.0
        rate_t0, rate_n, tx_rate = now, 0, 0.0
        bus_text = "OK"
        samples: list[Sample] = []
        try:
            while self._running:
                while True:
                    try:
                        fn, a = self._ops.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        fn(*a)
                    except Exception as e:
                        self._err(f"{type(e).__name__}: {e}")
                try:
                    rx = self.bus.recv_all()
                except Exception as e:
                    self._err(f"接收失败: {e}")
                    rx = []
                for f in rx:
                    if self.log_enabled:
                        self._frames.append(f)
                    smp = self._on_rx(f)
                    if smp is not None:
                        samples.append(smp)
                now = time.perf_counter()
                if self.sending:
                    if now >= next_tx:
                        period = 1.0 / max(1.0, self.rate_hz)
                        next_tx += period
                        if next_tx < now:
                            next_tx = now + period
                        if self._tick(now):
                            rate_n += 1
                else:
                    next_tx = now
                    self._last_out = NAN
                self._service_raw_periodic(now)
                self._service_config(now)
                self._service_gd(now)
                self._service_scan(now)
                if now >= next_bus_check:
                    next_bus_check = now + 1.0
                    try:
                        bus_text = self.bus.status_text()
                    except Exception as e:
                        bus_text = str(e)
                    tx_rate = rate_n / (now - rate_t0)
                    rate_t0, rate_n = now, 0
                if now >= next_emit:
                    next_emit = now + 1.0 / self.EMIT_HZ
                    if samples:
                        self.samples.emit(samples)
                        samples = []
                    if self._frames:
                        fr, self._frames = self._frames, []
                        fr.sort(key=lambda x: x.t)
                        self.frames.emit(fr)
                    self.state.emit(self._state(now, bus_text, tx_rate))
                time.sleep(0.0003)
        finally:
            if not gc.isenabled():
                gc.enable()
            if winmm is not None:
                winmm.timeEndPeriod(1)

    def _state(self, now: float, bus_text: str, tx_rate: float) -> dict:
        d = self.driver
        link_ok = self.last_fb is not None and (now - self.last_fb_t) < 0.2
        return {
            "driver": d,
            "code": self.motor_code if link_ok else -1,
            "enabled": self.active,
            "active": self.active,
            "sending": self.sending,
            "mode": self.mode,
            "link_ok": link_ok,
            "bus": bus_text,
            "tx_rate": tx_rate if self.sending else 0.0,
            "running": self._run_obj is not None,
            "scanning": self._scan is not None,
            "queue": len(self._reqs) + (1 if self._pending else 0) + len(self._gd_queue) + (1 if self._gd_pending else 0),
            "gd_busy": bool(self._gd_queue or self._gd_pending),
            "limits_ok": self.limits_ok,
            "blocked": self._config_blocked(),
            "tripped": self.safety.tripped,
            "trip_reason": self.safety.reason,
            "pos_mt": self.safety.pos,
            "lat": self.latency_stats(),
        }
