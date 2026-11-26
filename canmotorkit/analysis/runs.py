"""测试与闭环的「运行」对象：在 Worker 线程内按发送频率调用 output() 得到电流/扭矩指令，
并由 record() 记录反馈。单位按厂家约定（达妙/HM Nm，大疆 A）。

- ExciteRun：开环电流激励（阶跃/步进正弦/扫频/PRBS）。
- LoopRun：上位机速度 PI 或串级位置环（外位置 PID + 内速度 PID），参考信号为阶跃/斜坡/正弦。
"""
from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field

NAN = float("nan")


def _lowpass(last: float, value: float, cutoff: float, dt: float) -> float:
    if cutoff <= 0:
        return value
    rc = 1.0 / (2.0 * math.pi * cutoff)
    alpha = dt / (rc + dt)
    return last + alpha * (value - last)


class _Pid:
    """与 RMPP::Pid 对齐的单个 PID 状态机，仅供 LoopRun 使用。"""

    def __init__(self, kp: float, ki: float, kd: float, ff: float,
                 max_i: float, max_out: float, err_fc: float, d_fc: float):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.ff = ff
        self.max_i = max(0.0, max_i)
        self.max_out = max(0.0, max_out)
        self.err_fc = max(0.0, err_fc)
        self.d_fc = max(0.0, d_fc)
        self.err = 0.0
        self.last_err = 0.0
        self._first = True
        self.i_out = 0.0
        self.d_out = 0.0
        self.out = 0.0

    def components(self) -> tuple[float, float, float]:
        return self.kp * self.err, self.i_out, self.d_out

    def calculate(self, error: float, dt: float, derr: float | None = None) -> float:
        dt = min(max(dt, 1.0e-4), 0.01)
        if self._first:
            # 首拍：误差滤波器从当前误差起步，差分微分取 0，避免 kd·err/dt 的冲击。
            self._first = False
            self.err = self.last_err = error
        else:
            self.err = _lowpass(self.err, error, self.err_fc, dt)
        self.i_out = max(-self.max_i, min(self.max_i, self.i_out + self.ki * self.err * dt))
        d = self.kd * (derr if derr is not None else (self.err - self.last_err) / dt)
        self.d_out = _lowpass(self.d_out, d, self.d_fc, dt)
        self.last_err = self.err
        self.out = self.kp * self.err + self.i_out + self.d_out + self.ff
        if self.max_out <= 0.0:
            self.track(0.0)
        elif abs(self.out) > self.max_out:
            self.track(max(-self.max_out, min(self.max_out, self.out)))
        return self.out

    def track(self, actual: float):
        """按 RMPP 的 track() 回算积分，只向零收缩且不反向穿零。"""
        i = self.i_out + actual - self.out
        self.i_out = max(min(i, max(0.0, self.i_out)), min(0.0, self.i_out))
        self.out = actual


@dataclass
class Record:
    t: list = field(default_factory=list)
    u: list = field(default_factory=list)       # 实际下发电流/扭矩
    ref: list = field(default_factory=list)     # 激励或参考
    pos: list = field(default_factory=list)     # 多圈累计
    vel: list = field(default_factory=list)
    fb_out: list = field(default_factory=list)  # 反馈电流/扭矩
    seg: list = field(default_factory=list)     # 步进正弦段号（其它 -1）
    p_out: list = field(default_factory=list)   # 闭环内环 P 分量
    i_out: list = field(default_factory=list)   # 闭环内环 I 分量
    d_out: list = field(default_factory=list)   # 闭环内环 D 分量

    def __len__(self):
        return len(self.t)


class Run:
    title = ""

    def __init__(self):
        self.rec = Record()
        self._rec_lock = threading.Lock()
        self.t0 = 0.0
        self.ref = NAN
        self.done = False
        self.reason = ""
        self.seg = -1

    def start(self, t0: float, pos0: float, vel0: float):
        self.t0, self.pos0, self.vel0 = t0, pos0, vel0

    def output(self, now: float, fb, pos_mt: float) -> float | None:
        raise NotImplementedError

    def record(self, t: float, u: float, fb, pos_mt: float):
        with self._rec_lock:
            r = self.rec
            r.t.append(t - self.t0)
            r.u.append(u)
            r.ref.append(self.ref)
            r.pos.append(pos_mt)
            r.vel.append(fb.vel)
            r.fb_out.append(fb.out)
            r.seg.append(self.seg)
            r.p_out.append(NAN)
            r.i_out.append(NAN)
            r.d_out.append(NAN)

    def snapshot_record(self) -> Record:
        """复制一份一致的记录快照，供界面线程实时分析。"""
        with self._rec_lock:
            r = self.rec
            return Record(t=list(r.t), u=list(r.u), ref=list(r.ref), pos=list(r.pos),
                          vel=list(r.vel), fb_out=list(r.fb_out), seg=list(r.seg),
                          p_out=list(r.p_out), i_out=list(r.i_out), d_out=list(r.d_out))

    def finish(self, reason: str = ""):
        self.done = True
        self.reason = reason


# ============================ 开环激励 ============================
EXCITE_KINDS = {"step": "阶跃", "stepsine": "步进正弦", "chirp": "扫频", "prbs": "PRBS"}


def log_freqs(f0: float, f1: float, n: int) -> list[float]:
    if n <= 1:
        return [f0]
    return [f0 * (f1 / f0) ** (i / (n - 1)) for i in range(n)]


@dataclass
class ExciteConfig:
    kind: str = "stepsine"
    amp: float = 2.5            # 幅值（厂家单位）
    step_on: float = 0.3        # 阶跃持续时间 s
    step_delay: float = 0.0
    step_tail: float = 0.5      # 阶跃结束后继续记录 s（输出为 0）
    f0: float = 1.0
    f1: float = 30.0
    n_freq: int = 30
    cycles: int = 6             # 步进正弦每个频点的周期数（前 1 个丢弃作过渡）
    min_seg: float = 0.3        # 每个频点最短时长 s
    duration: float = 5.0       # 扫频 / PRBS 时长
    prbs_dt: float = 0.01       # PRBS 码元宽度 s
    seed: int = 1

    def label(self) -> str:
        k = EXCITE_KINDS[self.kind]
        if self.kind == "step":
            return f"{k} {self.amp:g}，持续 {self.step_on:g} s"
        return f"{k} ±{self.amp:g}，{self.f0:g}~{self.f1:g} Hz"


class ExciteRun(Run):
    def __init__(self, cfg: ExciteConfig):
        super().__init__()
        self.cfg = cfg
        self.title = "响应测试：" + cfg.label()
        self.segs: list[tuple[float, float, float]] = []   # (开始, 结束, 频率)
        if cfg.kind == "stepsine":
            t = 0.0
            for f in log_freqs(cfg.f0, cfg.f1, cfg.n_freq):
                dur = max(cfg.cycles / f, cfg.min_seg)
                dur = math.ceil(dur * f) / f          # 整数周期
                self.segs.append((t, t + dur, f))
                t += dur
            self.total = t
        elif cfg.kind == "step":
            self.total = cfg.step_delay + cfg.step_on + cfg.step_tail
        else:
            self.total = cfg.duration
        rnd = random.Random(cfg.seed)
        n = int(self.total / cfg.prbs_dt) + 2
        self._prbs = [1.0 if rnd.random() < 0.5 else -1.0 for _ in range(n)]

    def excitation(self, t: float) -> float:
        c = self.cfg
        self.seg = -1
        if c.kind == "step":
            return c.amp if c.step_delay <= t < c.step_delay + c.step_on else 0.0
        if c.kind == "stepsine":
            for i, (a, b, f) in enumerate(self.segs):
                if a <= t < b:
                    self.seg = i
                    return c.amp * math.sin(2 * math.pi * f * (t - a))
            return 0.0
        if c.kind == "chirp":
            T = self.total
            k = math.log(c.f1 / c.f0) / T          # 对数扫频
            ph = 2 * math.pi * c.f0 * (math.exp(k * t) - 1) / k
            return c.amp * math.sin(ph)
        if c.kind == "prbs":
            return c.amp * self._prbs[int(t / c.prbs_dt)]
        return 0.0

    def output(self, now, fb, pos_mt):
        t = now - self.t0
        if t >= self.total:
            self.ref = NAN
            return None
        x = self.excitation(t)
        self.ref = x
        return x


# ============================ 上位机闭环 ============================
LOOP_KINDS = {"vel": "速度环（PI）", "pos": "串级位置环", "pos_single": "单环位置环（PID）"}
REF_KINDS = {"step": "阶跃", "ramp": "斜坡", "sine": "正弦"}


@dataclass
class LoopConfig:
    loop: str = "vel"           # vel / pos / pos_single
    kp: float = 0.1
    ki: float = 0.0
    kd: float = 0.0
    ref_kind: str = "step"
    amp: float = 2.0            # 阶跃幅值 / 斜坡终值 / 正弦幅值（rad/s 或 rad）
    freq: float = 1.0           # 正弦频率
    ramp_time: float = 1.0
    delay: float = 0.0
    duration: float = 2.0
    out_max: float = 1.0        # 控制器输出限幅（另有安全限幅）
    integral_max: float = 1.0   # 积分项输出限幅，单位同 out_max
    ff: float = 0.0             # 速度环前馈
    err_fc: float = 0.0         # 速度环误差低通截止频率 Hz
    d_fc: float = 0.0           # 速度环微分低通截止频率 Hz
    pos_kp: float | None = None # 串级位置环外环 Kp，输出速度参考；None 为旧单环兼容
    pos_ki: float | None = None
    pos_kd: float | None = None
    pos_ff: float = 0.0
    pos_integral_max: float = 0.0
    pos_out_max: float = 50.0   # 外环输出速度上限 rad/s
    pos_err_fc: float = 0.0
    pos_d_fc: float = 0.0
    relative: bool = True       # 位置环参考相对起始位置
    d_on_meas: bool = True      # 位置环微分作用于测量速度（避免阶跃冲击）
    # 空列表表示沿用旧版的 amp/duration 单次阶跃配置。
    step_values: list[float] = field(default_factory=list)
    step_durations: list[float] = field(default_factory=list)

    def label(self) -> str:
        kd = f" Kd={self.kd:g}" if self.loop in ("pos", "pos_single") else ""
        if self.ref_kind == "step":
            values = self.step_values or [self.amp]
            durations = self.step_durations or [self.duration]
            steps = ", ".join(f"{v:g}/{d:g}s" for v, d in zip(values, durations))
            ref = f"{REF_KINDS[self.ref_kind]} [{steps}]"
        else:
            ref = f"{REF_KINDS[self.ref_kind]} {self.amp:g}"
        return f"{LOOP_KINDS[self.loop]} {ref} Kp={self.kp:g} Ki={self.ki:g}{kd}"


class LoopRun(Run):
    def __init__(self, cfg: LoopConfig):
        super().__init__()
        self.cfg = cfg
        self.title = "闭环测试：" + cfg.label()
        self.inner_pid = _Pid(cfg.kp, cfg.ki, cfg.kd, cfg.ff, cfg.integral_max,
                              cfg.out_max, cfg.err_fc, cfg.d_fc)
        self.legacy_position = cfg.loop == "pos_single" or (cfg.loop == "pos" and cfg.pos_kp is None)
        self.outer_pid = _Pid(cfg.pos_kp or 0.0, cfg.pos_ki or 0.0, cfg.pos_kd or 0.0, cfg.pos_ff,
                              cfg.pos_integral_max, cfg.pos_out_max,
                              cfg.pos_err_fc, cfg.pos_d_fc)
        self.prev_t: float | None = None
        self.sat = 0
        self.n = 0
        self.step_values = cfg.step_values or [cfg.amp]
        self.step_durations = cfg.step_durations or [cfg.duration]
        self.total = (sum(self.step_durations) if cfg.ref_kind == "step"
                      else cfg.duration)

    def reference(self, t: float) -> float:
        c = self.cfg
        if c.ref_kind == "step":
            elapsed = 0.0
            x = self.step_values[-1] if self.step_values else 0.0
            for value, duration in zip(self.step_values, self.step_durations):
                if t < elapsed + duration:
                    x = value
                    break
                elapsed += duration
        elif c.ref_kind == "ramp":
            # 默认无延迟；若调用方显式设置 delay，则从延迟结束后连续爬坡。
            elapsed = max(0.0, t - c.delay)
            x = c.amp * min(1.0, elapsed / max(c.ramp_time, 1e-6))
        else:
            elapsed = max(0.0, t - c.delay)
            x = c.amp * math.sin(2 * math.pi * c.freq * elapsed)
        if c.loop in ("pos", "pos_single") and c.relative:
            x += self.pos0
        return x

    def output(self, now, fb, pos_mt):
        c = self.cfg
        t = now - self.t0
        if t >= self.total:
            self.ref = NAN
            return None
        if fb is None:
            return 0.0
        if c.loop != "pos_single" and not math.isfinite(fb.vel):
            # 6623 等需估算速度的电机刚开始时速度为 NaN，不能进入积分器（会永久变 NaN）。
            return 0.0
        r = self.reference(t)
        self.ref = r
        dt = 0.0 if self.prev_t is None else max(0.0, now - self.prev_t)
        self.prev_t = now
        if c.loop == "vel":
            u = self.inner_pid.calculate(r - fb.vel, dt)
        elif self.legacy_position:
            # 微分作用于测量：d(err)/dt ≈ -ω（参考阶跃不产生冲击）；否则对误差差分。
            derr = -fb.vel if c.d_on_meas and math.isfinite(fb.vel) else None
            u = self.inner_pid.calculate(r - pos_mt, dt, derr)
        else:
            derr = -fb.vel if c.d_on_meas and math.isfinite(fb.vel) else None
            speed_ref = self.outer_pid.calculate(r - pos_mt, dt, derr)
            u = self.inner_pid.calculate(speed_ref - fb.vel, dt)
        self.n += 1
        if abs(u) >= self.inner_pid.max_out > 0:
            self.sat += 1
        return u

    def record(self, t: float, u: float, fb, pos_mt: float):
        super().record(t, u, fb, pos_mt)
        self.rec.p_out[-1], self.rec.i_out[-1], self.rec.d_out[-1] = self.inner_pid.components()

    def components(self) -> tuple[float, float, float]:
        return self.inner_pid.components()

    def track_output(self, actual: float):
        """由 Worker 在安全/电机限幅后回传实际输出，复现 RMPP 的积分跟踪。"""
        self.inner_pid.track(actual)

    @property
    def sat_ratio(self) -> float:
        return self.sat / self.n if self.n else 0.0
