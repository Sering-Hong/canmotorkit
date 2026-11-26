"""闭环 PID 的纯逻辑检查。"""
import math

from canmotorkit.analysis.runs import LoopConfig, LoopRun, _Pid
from canmotorkit.drivers.base import Feedback


def test_pid_first_sample_has_no_derivative_kick():
    pid = _Pid(kp=0.0, ki=0.0, kd=1.0, ff=0.0, max_i=0.0, max_out=1000.0, err_fc=0.0, d_fc=0.0)
    assert pid.calculate(1.0, 0.0) == 0.0          # 旧实现为 kd·1/1e-4 = 1e4（被限到 max_out）
    out = pid.calculate(1.5, 0.001)
    assert abs(out - 500.0) < 1e-6


def test_position_loop_derivative_on_error_when_not_on_measurement():
    cfg = LoopConfig(loop="pos_single", kp=0.0, ki=0.0, kd=0.1, ref_kind="ramp", amp=1.0,
                     ramp_time=1.0, duration=1.0, out_max=10.0, d_on_meas=False)
    run = LoopRun(cfg)
    run.start(0.0, 0.0, 0.0)
    fb = Feedback(pos=0.0, vel=0.0, out=0.0)
    run.output(0.001, fb, 0.0)
    u = run.output(0.002, fb, 0.0)
    # 斜坡 1 rad/s、位置不动 → de/dt≈1 → u≈kd；旧实现恒为 0
    assert abs(u - 0.1) < 1e-6


def test_velocity_loop_ignores_nan_velocity():
    run = LoopRun(LoopConfig(loop="vel", kp=0.1, ki=1.0, duration=1.0, out_max=1.0, integral_max=1.0))
    run.start(0.0, 0.0, 0.0)
    assert run.output(0.001, Feedback(pos=0.0, vel=float("nan"), out=0.0), 0.0) == 0.0
    u = run.output(0.002, Feedback(pos=0.0, vel=0.0, out=0.0), 0.0)
    assert math.isfinite(u) and math.isfinite(run.inner_pid.i_out)
