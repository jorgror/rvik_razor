# RVik Razor

RVik Razor manages controllable electrical consumption so a home can stay near a configured hourly energy set point.

## Language

**Load**:
A controllable electrical consumer whose consumption can be reduced or restored by RVik Razor.

**Eligible load**:
A load that is available for regulation in the current control cycle.

**Priority**:
The configured order in which loads are considered for regulation. Lower priority numbers are considered first for reduction; higher priority numbers are considered first for restoration.

**Hourly set point**:
The configured target energy consumption for the current clock hour.
_Avoid_: Hourly limit, effective target

**Projection**:
The expected energy consumption at the end of the current clock hour.

**Power estimate**:
An estimate of a load's current or candidate electrical consumption.

**Accuracy**:
How close projected hourly consumption is to the hourly set point.
_Avoid_: Safety margin

**Accuracy deadband**:
The minimum accuracy improvement required before RVik Razor changes a load.
_Avoid_: Restore margin

**Control direction**:
Whether the current cycle is trying to reduce or restore consumption based on the projection relative to the hourly set point.

**Reduce**:
Move a load toward lower electrical consumption.
_Avoid_: Stop, turn off

**Late-hour load**:
A switch load that runs at the end of each hour instead of being switched to hit the hourly set point. It starts only once it can run at full power until the hour ends.

**Vehicle amp trim**:
Fitting the charge current of the vehicle charging on a late-hour load to the room left in the hour, instead of only switching the charger on and off.

**Charging vehicle**:
The one vehicle RVik Razor has identified as charging on a late-hour load during the current plug-in. It must be online, charging, at home and drawing about the charger's measured power.

**Restore**:
Move a load toward higher electrical consumption.
_Avoid_: Start, turn on
