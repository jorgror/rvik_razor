# Use Horizon Set-Point Control for Load Regulation

RVik Razor will regulate loads by projecting candidate load states to the end of the current hour and choosing actions that improve accuracy to the hourly set point. This replaces the previous effective-target strategy with an early-hour fraction and final-window jump, because users should tune practical reserve by adjusting the hourly set point itself rather than by relying on hidden controller conservatism.

**Consequences**

The controller may accept a small projected overshoot when reducing an eligible load would make the end-of-hour projection less accurate. Priority remains a hard ordering rule: lower priority numbers are considered first for reduction, higher priority numbers are considered first for restoration, and a bad fit at the first eligible priority causes the controller to wait rather than act on a better-fitting lower-priority candidate.
