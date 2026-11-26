"""用演示台（DemoBus）对 Worker 做无界面集成测试，覆盖达妙/HM 与大疆。"""
import math
import time

import pytest
from PySide6.QtCore import QCoreApplication

from canmotorkit.analysis import sysid
from canmotorkit.analysis.runs import ExciteConfig, ExciteRun, LoopConfig, LoopRun
from canmotorkit.control.safety import SafetyLimits
from canmotorkit.core.worker import Worker
from canmotorkit.core.bus import Frame
from canmotorkit.drivers import dm_protocol as P
from canmotorkit.drivers import gd
from canmotorkit.drivers.catalog import make_driver
from canmotorkit.sim.demo import DemoBus, DjiSimMotor, DmSimMotor, GdSimMotor


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def pump(app, sec, until=None):
    end = time.perf_counter() + sec
    while time.perf_counter() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.005)
    return bool(until and until())


class Rig:
    def __init__(self, app, driver=None):
        self.bus = DemoBus()
        self.bus.open()
        self.w = Worker(self.bus, driver)
        self.rec = {"samples": [], "regs": [], "scan": None, "msgs": [], "runs": [], "tx": {}}
        w, r = self.w, self.rec
        w.samples.connect(lambda s: r["samples"].extend(s))
        w.reg_done.connect(lambda k, rid, v, e: r["regs"].append((k, rid, v, e)))
        w.scan_done.connect(lambda f: r.__setitem__("scan", f))
        w.message.connect(r["msgs"].append)
        w.run_finished.connect(r["runs"].append)
        w.frames.connect(self._frames)
        w.start()

    def _frames(self, fs):
        for f in fs:
            if f.tx:
                self.rec["tx"][f.can_id] = self.rec["tx"].get(f.can_id, 0) + 1

    def close(self):
        self.w.stop()
        self.w.wait(2000)
        self.bus.close()


@pytest.fixture
def hm(app):
    r = Rig(app, make_driver("dm", "HM9816G9", 1, 0))
    r.w.set_sending(True)
    assert pump(app, 1.5, lambda: r.w.limits_ok and r.w.last_fb is not None)
    yield r
    r.close()


def test_dm_limits_and_registers(app, hm):
    w, rec = hm.w, hm.rec
    assert abs(w.lim.p_max - 3.14159265) < 1e-6
    w.read_reg(0x3C)
    w.read_reg(0x0A)            # HM 没有 CTRL_MODE → 超时
    w.write_reg(0x09, 0)
    w.write_reg(0x14, 5.0)      # 本软件只读
    w.save()
    assert pump(app, 2.0, lambda: len(rec["regs"]) >= 5 + 3)
    d = {(k, r): (v, e) for k, r, v, e in rec["regs"]}
    assert abs(d[("read", 0x3C)][0] - 24.0) < 0.2
    assert d[("read", 0x0A)][1]
    assert d[("write", 0x14)][1]
    assert d[("save", 0)] == (True, "")


def test_dm_j6006_uses_common_mit_protocol(app):
    driver = make_driver("dm", "DM-J6006-2EC", 5, 0x15)
    assert driver.family == "dm"
    assert driver.caps.mit and not driver.caps.registers
    assert driver.ready and not driver.limits_from_motor
    assert driver.title() == "DM-J6006-2EC  ID 5 / Master 0x15"
    assert driver.output_frames(0.5)[0][0] == 5
    status = P.pack_status(5, P.CODE_DISABLED, 0.2, -1.5, 0.3, 35, 36, P.DEFAULT_LIMITS)
    fb = driver.parse(0x15, status)
    assert fb is not None and fb.motor_id == 5
    assert fb.vel == pytest.approx(-1.5, abs=0.02)


def test_dm_current_mode_estop_and_disable(app, hm):
    w, bus = hm.w, hm.bus
    m = bus.motor(DmSimMotor, 1)
    w.set_target_out(0.5)                       # 低于静摩擦 0.8，不会转
    w.enable()
    assert pump(app, 0.5, lambda: w.active)
    pump(app, 0.2)
    assert m.cmd.kp == 0 and m.cmd.kd == 0 and abs(m.cmd.t - 0.5) < 0.03
    assert abs(m.plant.vel) < 1e-6
    w.estop()
    pump(app, 0.2)
    assert m.enabled and w.safety.tripped and abs(m.cmd.t) < 0.03    # 急停不失能
    w.reset_safety()
    pump(app, 0.2)
    assert not w.safety.tripped and abs(m.cmd.t - 0.5) < 0.03
    w.disable()
    assert pump(app, 0.5, lambda: not m.enabled and w.motor_code == 0)
    n = hm.rec["tx"].get(P.CONFIG_ID, 0)
    pump(app, 0.3)
    assert hm.rec["tx"].get(P.CONFIG_ID, 0) > n + 20      # 失能后恢复 0xCC 查询


def test_dm_mit_mode(app, hm):
    w, m = hm.w, hm.bus.motor(DmSimMotor, 1)
    w.set_mode("mit")
    w.set_target_mit(P.MitCommand(p=0.5, kp=20, kd=1))
    w.enable()
    assert pump(app, 2.0, lambda: w.active and abs(w.last_fb.pos - 0.5) < 0.05)
    w.disable()


def test_dm_mit_equivalent_torque_respects_output_limit(app, hm):
    """kp·Δp 产生的扭矩也必须受左侧扭矩限幅约束（原来只限前馈 t）。"""
    w, m = hm.w, hm.bus.motor(DmSimMotor, 1)
    w.set_safety_limits(SafetyLimits(vel_max=20.0, out_max=1.0, temp_max=0))
    w.set_mode("mit")
    w.set_target_mit(P.MitCommand(p=1.5, kp=100, kd=0.5))    # 不限幅时约 150 Nm
    w.enable()
    peak = [0.0]

    def watch():
        peak[0] = max(peak[0], abs(m.tau))
        return False
    pump(app, 1.0, watch)
    assert w.active and peak[0] > 0.5
    assert peak[0] <= 1.0 + 0.2, peak[0]       # 允许一个通信周期的估算滞后
    w.disable()


def test_overspeed_trip(app, hm):
    w, m = hm.w, hm.bus.motor(DmSimMotor, 1)
    w.set_safety_limits(SafetyLimits(vel_max=2.0, out_max=2.0, temp_max=0))
    w.set_target_out(1.5)
    w.enable()
    assert pump(app, 3.0, lambda: w.safety.tripped)
    assert "速度" in w.safety.reason
    pump(app, 0.1)
    assert m.enabled and abs(m.cmd.t) < 0.03
    w.disable()


def test_dji_current(app):
    r = Rig(app, make_driver("dji", "M3508 (C620)", 1))
    try:
        w, m = r.w, r.bus.motor(DjiSimMotor, 1)
        assert pump(app, 0.5, lambda: w.last_fb is not None)     # 广播反馈，无需发送
        assert r.rec["tx"] == {}
        w.set_target_out(2.0)
        pump(app, 0.05)
        w.enable()
        assert pump(app, 0.3, lambda: abs(m.cur - 1.0) < 0.01)    # 默认安全限幅 ±1
        w.set_safety_limits(SafetyLimits(vel_max=100, out_max=5, temp_max=0))
        w.enable()                                               # 软件使能 + 自动开发送
        assert pump(app, 0.5, lambda: abs(m.cur - 2.0) < 0.01)
        assert pump(app, 1.0, lambda: w.last_fb.vel > 1.0)
        assert pump(app, 0.5, lambda: abs(w.last_fb.out - 2.0) < 0.2)
        w.disable()
        assert pump(app, 0.3, lambda: m.cur == 0.0)
    finally:
        r.close()


def test_dji_6623_protocol(app):
    driver = make_driver("dji", "RM 6623", 0)
    assert driver.ctrl == (0x1FF, 0)
    assert driver.fb_id == 0x205
    assert driver.encode(5000) == 5000
    assert driver.encode(-5000) == -5000
    frame = driver.output_frames(1234)[0]
    assert frame == (0x1FF, bytes.fromhex("04 D2 00 00 00 00 00 00"))
    fb = driver.parse(0x205, bytes.fromhex("10 00 04 D2 FB 2E 00 00"))
    assert fb is not None
    assert fb.out == 1234
    assert fb.vel != fb.vel


def test_dji_6623_velocity_estimate_uses_time_window(app):
    """6623 无 RPM 字段时，角度差分应稳定且能跨过单圈边界。"""
    driver = make_driver("dji", "RM 6623", 0)
    w = Worker(DemoBus(), driver)
    speeds = []
    # 2 rad/s，1 kHz 反馈；起点放在 +π 附近，强制覆盖回绕。
    for i in range(120):
        pos = math.pi - 0.02 + 2.0 * i * 0.001
        raw = int((pos % (2 * math.pi)) / (2 * math.pi) * 8192) & 0x1FFF
        fb = w._on_rx(Frame(i * 0.001, 0x205,
                             raw.to_bytes(2, "big") + bytes(6)))
        if fb is not None and math.isfinite(fb.fb.vel):
            speeds.append(fb.fb.vel)
    assert speeds
    assert max(abs(v - 2.0) for v in speeds[-30:]) < 0.15
    assert max(abs(v) for v in speeds) < 10.0

    # 同一批次中时间戳几乎相同的帧不能制造数千 rad/s 的尖峰。
    w = Worker(DemoBus(), driver)
    for i in range(20):
        pos = 0.5 + i * 0.001
        raw = int((pos % (2 * math.pi)) / (2 * math.pi) * 8192) & 0x1FFF
        fb = w._on_rx(Frame(i * 0.00001, 0x205,
                             raw.to_bytes(2, "big") + bytes(6)))
        assert fb is None or not math.isfinite(fb.fb.vel) or abs(fb.fb.vel) < 100.0


def test_scan_all_protocols(app):
    r = Rig(app)
    try:
        r.w.start_scan(range(1, 8), window=0.01, listen=0.2)
        assert pump(app, 4.0, lambda: r.rec["scan"] is not None)
        got = {(x["protocol"], x["model"], x["motor_id"]) for x in r.rec["scan"]}
        assert ("dm", "HM9816G9", 1) in got
        assert ("dm", "DM-J8009P", 3) in got
        assert ("gd", gd.MODEL_5036_FAMILY, 6) in got
        assert ("gd", gd.MODEL_6036, 7) in got
        dji = {x["fb_id"]: x for x in r.rec["scan"] if x["protocol"] == "dji"}
        assert set(dji) == {0x201, 0x202, 0x205, 0x206}
        assert dji[0x202]["model"] == "M2006 (C610)"           # 无温度 → C610
        assert dji[0x206]["model"] in ("M3508 (C620)", "GM6020 (电流模式)")
        assert len(dji[0x206]["candidates"]) == 4                # 含 RM 6623 的重叠候选
        assert ("RM 6623", 0) in dji[0x205]["candidates"]
        dm8 = next(x for x in r.rec["scan"] if x["motor_id"] == 3 and x["protocol"] == "dm")
        assert dm8["master_id"] == 0x13
    finally:
        r.close()


def test_gd_output_mit_estop_fault_and_disable(app):
    r = Rig(app, make_driver("gd", gd.MODEL, 6))
    try:
        w, m = r.w, r.bus.motor(GdSimMotor, 6)
        w.set_sending(True)
        assert pump(app, 1, lambda: w.last_fb is not None)
        assert w.last_fb.code == 0 and not w.active
        assert pump(app, 1, lambda: not w._gd_queue and w._gd_pending is None)
        w.set_safety_limits(SafetyLimits(vel_max=100, out_max=1, temp_max=0))
        w.enable()
        w.set_target_out(0.2)
        assert pump(app, 1, lambda: w.active and abs(w.last_fb.out - 0.2) < 0.02)
        assert all(f.extended for f in w._frames if f.can_id == 0x18006)
        w.zero_output()
        assert pump(app, 1, lambda: m.cmd.t == 0 and w.get_target_out() == 0)
        w.set_mode("mit")
        w.set_target_mit(P.MitCommand(p=0.1, kp=10, kd=1))
        assert pump(app, 1, lambda: m.cmd.kp > 9)
        w.estop()
        assert pump(app, 1, lambda: w.safety.tripped and m.cmd == P.MitCommand())
        assert w.active and not m.stopped  # 急停为零扭矩，不发刹车/自由停转
        w.set_mode("current")
        w.reset_safety()
        assert pump(app, 1, lambda: not w.safety.tripped)
        run = ExciteRun(ExciteConfig(kind="step", amp=0.2, step_delay=0.02, step_on=0.08, step_tail=0.05))
        w.start_run(run)
        assert pump(app, 2, lambda: run.done)
        assert not run.reason and len(run.rec.t) > 20
        loop = LoopRun(LoopConfig(loop="vel", kp=0.05, ki=0.1, amp=0.5, duration=0.2, out_max=0.5))
        w.start_run(loop)
        assert pump(app, 2, lambda: loop.done)
        assert not loop.reason and len(loop.rec.t) > 20
        m.error = 2
        assert pump(app, 1, lambda: w.safety.tripped and w.last_fb.code == 2)
        w.disable()
        assert pump(app, 1, lambda: not w.active and m.stopped)
    finally:
        r.close()


@pytest.mark.parametrize("kind", ["step", "stepsine", "chirp", "prbs", "vel", "pos", "pos_single"])
def test_gd_all_response_and_closed_loop_modes(app, kind):
    r = Rig(app, make_driver("gd", gd.MODEL_5036, 6))
    try:
        w = r.w
        assert pump(app, 1, lambda: not w._gd_queue and w._gd_pending is None)
        w.set_safety_limits(SafetyLimits(vel_max=100, out_max=0.5, temp_max=0))
        w.enable()
        assert pump(app, 1, lambda: w.active and w.last_fb is not None)
        if kind in ("vel", "pos", "pos_single"):
            run = LoopRun(LoopConfig(loop=kind, kp=0.2, ki=0.1, kd=0.01, amp=0.05,
                                     pos_kp=2, pos_ki=0, pos_kd=0, duration=0.15, out_max=0.5))
        else:
            run = ExciteRun(ExciteConfig(kind=kind, amp=0.2, f0=10, f1=20, n_freq=2,
                                         cycles=2, min_seg=0.1, duration=0.15,
                                         step_delay=0.01, step_on=0.08, step_tail=0.06))
        w.start_run(run)
        assert pump(app, 2, lambda: run.done)
        assert not run.reason, run.reason
        assert len(run.rec) > 15
        assert all(abs(u) <= 0.5 for u in run.rec.u)
        assert pump(app, 1, lambda: r.bus.motor(GdSimMotor, 6).cmd.t == 0)
    finally:
        r.close()


def test_gd_settings_periodic_reboot_and_id_on_demo(app):
    r = Rig(app, make_driver("gd", gd.MODEL, 6))
    results = []
    r.w.gd_done.connect(lambda k, v, e: results.append((k, v, e)))
    try:
        w, m = r.w, r.bus.motor(GdSimMotor, 6)
        assert pump(app, 1, lambda: not w._gd_queue and w._gd_pending is None)
        assert w.driver.model == gd.MODEL_5036_FAMILY
        assert w.driver.identity['firmware'] == '0.1.8'
        for kind, value in (("periodic", 1000), ("periodic", 0), ("save", None),
                            ("zero", None), ("reboot", None), ("change_id", 9)):
            count = len(results)
            w.gd_request(kind, value)
            assert pump(app, 2, lambda: any(item[0] == kind for item in results[count:]))
            assert not results[-1][2], results[-1]
            if kind == 'periodic':
                assert m.period_us == value
                if value:
                    assert pump(app, 1, lambda: w.last_fb is not None)
            if kind == 'save':
                assert m.saved
        assert w.driver.motor_id == 9 and m.motor_id == 9
        w.set_sending(True)
        assert pump(app, 1, lambda: w.last_fb is not None and w.last_fb.motor_id == 9)
        w.enable()
        assert pump(app, 1, lambda: w.active)
        w.set_mode('gd')
        w.set_gd_control('position_velocity_torque', dict(position=.1, velocity=1, torque=.4))
        assert pump(app, 1, lambda: m.mode == 'position_velocity_torque')
        w.zero_output()
        assert pump(app, 1, lambda: m.mode == 'torque' and m.cmd.t == 0)
    finally:
        r.close()


def test_response_and_loop_on_demo(app):
    r = Rig(app, make_driver("dji", "M3508 (C620)", 1))
    try:
        w = r.w
        w.rate_hz = 500
        w.set_safety_limits(SafetyLimits(vel_max=40, out_max=5, temp_max=0, fb_timeout=0.1))
        w.enable()
        assert pump(app, 0.5, lambda: w.active)
        run = ExciteRun(ExciteConfig(kind="stepsine", amp=1.5, f0=2, f1=20, n_freq=5, cycles=4,
                                     min_seg=0.2))
        w.start_run(run)
        assert pump(app, 8.0, lambda: run.done), r.rec["msgs"]
        assert not run.reason, run.reason
        bode = sysid.bode_from_run(run)
        assert bode is not None
        model = sysid.fit_model(bode, "A")
        J_true = 0.002 / 0.3                 # 等效 J/k（A 单位）
        assert 0.4 < model.J / J_true < 2.0, model
        s = sysid.suggest(model, latency=0.002, rate_hz=500)
        loop = LoopRun(LoopConfig(loop="vel", kp=s.vel_kp * 0.5, ki=s.vel_ki * 0.5, amp=10.0,
                                  duration=1.5, out_max=5))
        w.start_run(loop)
        assert pump(app, 4.0, lambda: loop.done)
        st = sysid.loop_stats(loop.rec, "vel")
        assert st is not None and abs(st.ss_err) < 1.0, st
        w.disable()
    finally:
        r.close()
