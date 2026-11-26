"""安全监督：速度 / 位置（多圈累计）/ 电流或扭矩 / 温度 / 反馈看门狗。

约定（见 AGENTS.md）：
- 超限或急停时输出零电流/零扭矩，不失能（达妙/HM 失能会猛刹车）。
- 触发后锁存（tripped），必须用户显式 reset 才恢复输出。
- 单位按厂家报文约定：电流限制即对 MIT t（Nm）或大疆电流（A）的限幅。
纯逻辑，无 Qt / IO，便于测试。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SafetyLimits:
    vel_max: float = 10.0          # |速度| 上限 rad/s
    pos_enabled: bool = False      # 是否启用位置窗口
    pos_min: float = -6.0          # 多圈累计位置窗口 rad（相对累计原点）
    pos_max: float = 6.0
    out_max: float = 1.0           # |电流/扭矩指令| 限幅（厂家单位）
    temp_max: float = 100.0        # ℃；0 = 不检查
    fb_timeout: float = 0.1        # 输出非零时反馈超时 s；0 = 不检查
    vel_enabled: bool = True       # 是否启用速度限幅
    out_enabled: bool = True       # 是否启用当前协议对应的输出限幅


class MultiTurn:
    """把单圈位置（在 [-span/2, span/2) 内回绕）累计为多圈位置。"""

    def __init__(self, span: float = 2 * math.pi):
        self.span = span
        self.last: float | None = None
        self.turns = 0

    def reset(self):
        self.last = None
        self.turns = 0

    def update(self, raw: float) -> float:
        if self.last is not None:
            d = raw - self.last
            if d > self.span / 2:
                self.turns -= 1
            elif d < -self.span / 2:
                self.turns += 1
        self.last = raw
        return raw + self.turns * self.span


class Supervisor:
    def __init__(self, limits: SafetyLimits | None = None):
        self.limits = limits or SafetyLimits()
        self.mt = MultiTurn()
        self.tripped = False
        self.reason = ""
        self.pos = 0.0              # 多圈累计位置
        self.last_fb_t: float | None = None

    def set_wrap_span(self, span: float):
        """反馈位置回绕周期。达妙/HM 单圈 2π；若 p_max>π 则按 2*p_max。"""
        self.mt.span = span
        self.mt.reset()

    def trip(self, reason: str):
        if not self.tripped:
            self.tripped = True
            self.reason = reason

    def reset(self) -> bool:
        """用户解除锁存。若当前仍超限则拒绝。"""
        why = self._check_state(self._vel, self._temp)
        if why:
            self.reason = why
            return False
        self.tripped = False
        self.reason = ""
        return True

    _vel = 0.0
    _temp = 0.0

    def _check_state(self, vel: float, temp: float) -> str:
        L = self.limits
        if L.vel_enabled and L.vel_max > 0 and abs(vel) > L.vel_max:
            return f"速度超限 {vel:+.3f} rad/s（限 ±{L.vel_max:g}）"
        if L.pos_enabled and not (L.pos_min <= self.pos <= L.pos_max):
            return f"位置超限 {self.pos:+.3f} rad（窗口 [{L.pos_min:g}, {L.pos_max:g}]）"
        if L.temp_max > 0 and temp > L.temp_max:
            return f"温度超限 {temp:.0f} ℃（限 {L.temp_max:g}）"
        return ""

    def on_feedback(self, t: float, raw_pos: float, vel: float, temp: float):
        self.last_fb_t = t
        self.pos = self.mt.update(raw_pos)
        self._vel, self._temp = vel, temp
        why = self._check_state(vel, temp)
        if why:
            self.trip(why)

    def zero_position(self):
        """把当前累计位置作为位置窗口的参考零点。"""
        if self.mt.last is not None:
            self.mt.turns = 0
            self.pos = self.mt.last

    def arm_watchdog(self, now: float):
        """开始输出时启动反馈看门狗：即使一帧反馈都没收到，超时也要触发。"""
        if self.last_fb_t is None or now > self.last_fb_t:
            self.last_fb_t = now

    def check_timeout(self, now: float, driving: bool = True):
        """电机可能出力（driving）时检查反馈超时。"""
        L = self.limits
        if (not self.tripped and driving and L.fb_timeout > 0 and self.last_fb_t is not None
                and now - self.last_fb_t > L.fb_timeout):
            self.trip(f"反馈超时 >{L.fb_timeout * 1000:.0f} ms")

    def filter_output(self, now: float, out: float) -> float:
        """返回允许输出的电流/扭矩。已触发 → 0；否则限幅。"""
        L = self.limits
        self.check_timeout(now, out != 0.0)
        if self.tripped:
            return 0.0
        if L.out_enabled and L.out_max > 0:
            out = max(-L.out_max, min(L.out_max, out))
        return out

    def filter_mit(self, now: float, p: float, v: float, kp: float, kd: float, t: float,
                   pos: float | None, vel: float | None) -> tuple[float, float, float, float, float, bool]:
        """MIT 指令的安全过滤，返回 (p, v, kp, kd, t, 是否被限幅)。

        电机端实际扭矩 τ = kp·(p−θ) + kd·(v−ω) + t，只限前馈 t 无法约束 kp/kd 产生的扭矩。
        这里用最近一次反馈 (θ, ω) 估算 τ：
        - 速度目标 v 限制在速度上限内；
        - 先把前馈 t 限到 ±out_max，再在 kp>0 时把位置目标 p 拉向 θ，使估算 τ 落在 ±out_max；
          kp=0 时只能缩减阻尼项不可控部分，退而把 t 反向补偿。
        估算基于上一帧反馈，存在一个通信周期的滞后，电机内部仍有自身 t_max 限幅。
        已锁存或反馈超时 → 全 0（零扭矩）。
        """
        L = self.limits
        driving = kp != 0.0 or kd != 0.0 or t != 0.0
        self.check_timeout(now, driving)
        if self.tripped:
            return 0.0, 0.0, 0.0, 0.0, 0.0, False
        limited = False
        if L.vel_enabled and L.vel_max > 0 and abs(v) > L.vel_max:
            v = max(-L.vel_max, min(L.vel_max, v))
            limited = True
        if not (L.out_enabled and L.out_max > 0):
            return p, v, kp, kd, t, limited
        m = L.out_max
        if abs(t) > m:
            t = max(-m, min(m, t))
            limited = True
        if pos is None or vel is None or not (math.isfinite(pos) and math.isfinite(vel)):
            return p, v, kp, kd, t, limited
        damp = kd * (v - vel)
        tau = kp * (p - pos) + damp + t
        if abs(tau) <= m:
            return p, v, kp, kd, t, limited
        target = math.copysign(m, tau)
        if kp > 0:
            p = pos + (target - damp - t) / kp
        else:
            t_new = max(-m, min(m, target - damp))
            if abs(damp + t_new) <= m:
                t = t_new
            elif v != vel:
                # 阻尼项单独已超限：保留前馈，减小 kd 使总扭矩落在限幅上。
                kd = max(0.0, (target - t) / (v - vel))
        return p, v, kp, kd, t, True
