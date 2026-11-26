import math

from canmotorkit.control.safety import MultiTurn, SafetyLimits, Supervisor


def test_multiturn_unwrap():
    m = MultiTurn()
    xs = [3.0, 3.1, -3.1, -3.0, 3.1, 3.0]   # 正向跨过 π 再反向回来
    ys = [m.update(x) for x in xs]
    assert abs(ys[2] - (2 * math.pi - 3.1)) < 1e-9
    assert abs(ys[4] - 3.1) < 1e-9


def test_output_clamp_and_trip_latch():
    s = Supervisor(SafetyLimits(vel_max=5, out_max=1.0, temp_max=0, fb_timeout=0))
    s.on_feedback(0.0, 0.0, 1.0, 30)
    assert s.filter_output(0.0, 3.0) == 1.0
    assert s.filter_output(0.0, -0.4) == -0.4
    s.on_feedback(0.01, 0.0, 6.0, 30)
    assert s.tripped and "速度" in s.reason
    assert s.filter_output(0.01, 0.5) == 0.0
    assert not s.reset()                       # 仍超速，拒绝解除
    s.on_feedback(0.02, 0.0, 0.5, 30)
    assert s.tripped                           # 锁存，不自动恢复
    assert s.reset() and not s.tripped


def test_position_window_multiturn():
    s = Supervisor(SafetyLimits(vel_max=0, pos_enabled=True, pos_min=-4, pos_max=4, temp_max=0,
                                fb_timeout=0))
    for x in (2.0, 3.0, -3.0, -2.5):        # 累计 ≈ 3.78
        s.on_feedback(0, x, 0, 0)
    assert not s.tripped and abs(s.pos - (2 * math.pi - 2.5)) < 1e-9
    s.on_feedback(0, -2.0, 0, 0)              # 累计 ≈ 4.28
    assert s.tripped and "位置" in s.reason


def test_feedback_timeout_only_when_output():
    s = Supervisor(SafetyLimits(vel_max=0, temp_max=0, fb_timeout=0.1))
    s.on_feedback(0.0, 0, 0, 0)
    assert s.filter_output(1.0, 0.0) == 0.0 and not s.tripped
    assert s.filter_output(1.0, 0.5) == 0.0 and s.tripped and "超时" in s.reason


def _tau(p, v, kp, kd, t, pos, vel):
    return kp * (p - pos) + kd * (v - vel) + t


def test_mit_equivalent_torque_limited_by_shifting_position():
    s = Supervisor(SafetyLimits(vel_max=5, out_max=1.0, temp_max=0, fb_timeout=0))
    s.on_feedback(0.0, 0.0, 0.0, 30)
    # kp=50、目标偏 1 rad → 估算 50 Nm，只限 t 时完全无约束
    p, v, kp, kd, t, limited = s.filter_mit(0.0, 1.0, 0.0, 50.0, 1.0, 0.0, pos=0.0, vel=0.0)
    assert limited and kp == 50.0 and kd == 1.0
    assert abs(_tau(p, v, kp, kd, t, 0.0, 0.0) - 1.0) < 1e-9
    p, v, kp, kd, t, limited = s.filter_mit(0.0, -1.0, 0.0, 50.0, 1.0, 0.0, pos=0.0, vel=0.0)
    assert abs(_tau(p, v, kp, kd, t, 0.0, 0.0) + 1.0) < 1e-9


def test_mit_within_limit_untouched_and_velocity_target_clamped():
    s = Supervisor(SafetyLimits(vel_max=5, out_max=1.0, temp_max=0, fb_timeout=0))
    s.on_feedback(0.0, 0.0, 0.0, 30)
    assert s.filter_mit(0.0, 0.01, 0.0, 10.0, 0.0, 0.5, 0.0, 0.0) == (0.01, 0.0, 10.0, 0.0, 0.5, False)
    p, v, kp, kd, t, limited = s.filter_mit(0.0, 0.0, 20.0, 0.0, 0.01, 0.0, 0.0, 0.0)
    assert v == 5.0 and limited


def test_mit_pure_damping_scales_kd():
    s = Supervisor(SafetyLimits(vel_max=0, vel_enabled=False, out_max=1.0, temp_max=0, fb_timeout=0))
    s.on_feedback(0.0, 0.0, 0.0, 30)
    p, v, kp, kd, t, limited = s.filter_mit(0.0, 0.0, 10.0, 0.0, 5.0, 0.0, 0.0, 0.0)
    assert limited and abs(_tau(p, v, kp, kd, t, 0.0, 0.0) - 1.0) < 1e-9


def test_mit_watchdog_counts_gains_and_tripped_zeroes():
    s = Supervisor(SafetyLimits(vel_max=0, temp_max=0, fb_timeout=0.1))
    s.on_feedback(0.0, 0.0, 0.0, 0)
    # t=0 但 kp≠0 仍会出力，反馈超时必须触发
    out = s.filter_mit(1.0, 0.5, 0.0, 10.0, 0.0, 0.0, 0.0, 0.0)
    assert s.tripped and out == (0.0, 0.0, 0.0, 0.0, 0.0, False)


def test_watchdog_armed_without_any_feedback():
    s = Supervisor(SafetyLimits(vel_max=0, temp_max=0, fb_timeout=0.1))
    assert s.filter_output(5.0, 0.5) == 0.5 and not s.tripped   # 未启动输出：不计时
    s.arm_watchdog(10.0)
    assert s.filter_output(10.05, 0.5) == 0.5
    assert s.filter_output(10.2, 0.5) == 0.0 and s.tripped
