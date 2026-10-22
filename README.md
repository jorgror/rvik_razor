# RVik Razor — Energy Limiter for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/jorgror/rvik_razor.svg)](https://github.com/jorgror/rvik_razor/releases)
[![License](https://img.shields.io/github/license/jorgror/rvik_razor.svg)](LICENSE)

RVik Razor automatically controls your EV charger and other loads to keep your **hourly energy consumption near a configurable set point** — protecting you from grid tariff peaks.

> **Works great with Zaptec, Easee, and any EV charger exposed as a `number` entity (ampere control), as well as any `switch`-based load (e.g. heat pump, water heater).**

---

## Features

### Core Functionality
- **Hourly Energy Monitoring**: Tracks current hour energy consumption from a sensor
- **Power Projection**: Estimates end-of-hour energy usage based on current power consumption
- **Automatic Load Control**: Reduces or restores loads when projected hourly consumption drifts from the set point
- **Smart Restoration**: Restores loads only when they can stay restored to the end of the hour without reducing accuracy

### Supported Load Types
1. **EV Charger (Ampere Control)**: Incrementally reduces charging amperage
2. **Switch Loads**: Turns switches on/off (e.g., heat pumps)

### Load Configuration Options
- **Priority**: Lower numbers are reduced first
- **Enabled**: Static on/off toggle for the load
- **Enabled Entity** (optional): Dynamic control via a binary sensor or input_boolean (e.g., only regulate EV charger when car is actually charging)
- **Run late in the hour** (switch loads): Run the load at the end of each hour instead of switching it to hit the set point (recommended for an on/off EV charger, see [Late-hour loads](#late-hour-loads))
- **Vehicle charge current entities** (late-hour switch loads, optional): The charge-current number entities of the vehicles that charge on this charger, for example `number.<car>_charge_current` from the Tesla Fleet integration. RVik Razor then trims the charging vehicle's current instead of only switching the charger (see [Vehicle amp trim](#vehicle-amp-trim))

### Operation Modes
- **Off**: All control disabled, loads restored to original state
- **Monitor**: Only monitors and reports, no control actions
- **Control**: Active control to stay near the hourly set point

### Configuration
- **Config Flow**: Full UI-based setup
- **Options Flow**: Add/remove loads, adjust priorities and set point
- **Load Priority**: Lower numbers are reduced first
- **Cooldown**: 120-second cooldown between load changes to prevent flapping

## Setup

### 1. Initial Configuration
1. Go to Settings → Devices & Services
2. Click "Add Integration"
3. Search for "RVik Razor"
4. Select your **Energy this hour** sensor (required)
5. Select your **House power** sensor (required for Control mode)
6. Set **Hourly set point**
7. Choose initial **Mode** (recommend starting with Monitor)

### 2. Add Loads
1. Go to the RVik Razor integration
2. Click "Configure"
3. Choose "Add a load"
4. Select load type:
   - **EV Charger**: Requires ampere control entity
   - **Switch**: Requires switch entity
5. Set priority (lower = cut first)
6. Optionally set:
   - **Enabled Entity**: Binary sensor to dynamically control load participation
   - **Power sensor** or **assumed power** for accurate regulation

### 3. Test and Monitor
1. Start in **Monitor** mode to observe behavior
2. Check the sensors:
   - **Current hour kWh**: Real-time consumption
   - **Projected end hour kWh**: Estimated end-of-hour total
   - **Projection error**: Difference between projected end-of-hour consumption and the set point
3. Switch to **Control** mode when ready

## Entities Created

### Control Entities
- `number.rvik_razor_max_hour_kwh`: Set the hourly energy set point
- `select.rvik_razor_mode`: Change operation mode (Off/Monitor/Control)

### Monitoring Sensors
- `sensor.rvik_razor_current_hour_kwh`: Current hour energy consumption
- `sensor.rvik_razor_projected_end_hour_kwh`: Projected end-of-hour consumption
  - Attributes: remaining_seconds, house_power_kw, last_action, last_action_reason, mode
- `sensor.rvik_razor_projection_error`: Difference between projected end-of-hour consumption and the set point

## How It Works

### Estimation Logic
```
projected_end_kwh = current_hour_kwh + (house_power_kw × remaining_seconds / 3600)
projection_error_kwh = projected_end_kwh - hourly_set_point_kwh
```

### Control Logic
1. **Direction first**:
   - If projection is above the set point by more than 0.02 kWh, try to reduce.
   - If projection is below the set point by more than 0.02 kWh, try to restore.
   - If projection is within 0.02 kWh, do nothing.

2. **Priority order**:
   - Reduction considers lower priority numbers first.
   - Restoration considers higher priority numbers first.
   - Disabled, unavailable, cooling-down, already-saturated, or unknown-power loads are skipped.
   - If the first eligible load would not improve accuracy enough, RVik Razor waits instead of trying a better-fitting lower-priority candidate.

3. **Candidate projection**:
   - For each candidate, RVik Razor subtracts the load's current estimated power and adds the candidate estimated power.
   - Switch loads are binary candidates.
   - EV chargers choose the ampere value in the requested direction that projects closest to the hourly set point.
   - Only one successful load action is executed per control cycle.

4. **Safety Features**:
   - 120-second cooldown per load between actions
   - Hour rollover detection and reset; control waits (up to 2 minutes) until the hour energy sensor has reset, since many meters reset it some seconds after the hour

### Late-hour loads

Switch loads marked **Run late in the hour** are left out of the set-point control above and follow their own rule ([ADR 0002](docs/adr/0002-run-binary-loads-late-in-the-hour.md)):

- **Start** once the load can run at full power until the end of the hour, with 0.6 kW to spare.
- **Stop** when the rest of the house no longer leaves room for it (up to 1.0 kW shortfall is accepted). The projection is never allowed more than 0.2 kWh above the set point while it runs.
- **Minimum times**: the load stays on and off for at least 5 minutes each; the 0.2 kWh cap overrides the minimum on time.

Consumption therefore happens late in each hour, where it can be cut if a shower or cooking starts. An on/off EV charger typically starts once per hour. The regulation timeout does not apply to late-hour loads.

### Vehicle amp trim

A late-hour charger can list the charge-current entities of the vehicles that charge on it ([ADR 0003](docs/adr/0003-trim-vehicle-charge-current.md)). The charger switch still starts and stops charging by the late-hour rule. While a vehicle charges, RVik Razor also fits its charge current to the room left in the hour:

- **Lower** the current at once when it no longer fits, down to 6 A. In the last 10 minutes of the hour, lower it 1 A further.
- **Raise** it, keeping 1 A spare, only when at least 2 A more has fitted for a minute, and at most every 5 minutes.
- **Stop** the charger by the late-hour stop rule when even 6 A does not fit, without lowering the current first.
- **Wait** a minute after each change and after the charger starts, while the vehicle follows. The charger can report its power later than the meter.

Vehicles draw very different power per ampere on the same charger, and one vehicle can change rate between charging stretches or after a lower current. On one 3-phase charger, a Tesla drew about 0.67 kW/A after it was plugged in, and about 0.4 kW/A after its first lower current or pause. Once the current has been steady for a minute, RVik Razor measures the bound vehicle's kW per ampere from the charger's power, and measures again after each change. The next start after a pause uses the rate the vehicle last drew. Until the first measurement, the load's **assumed power** is taken as its power at the maximum current, so set it for the most powerful vehicle (for example 11 kW).

To find which vehicle is charging, RVik Razor refreshes the listed vehicles once the charger draws at least 1 kW. It binds the one vehicle that is online, charging, at `home` and reporting about the charger's measured power. With no clear match, it retries up to 3 times per plug-in, 5 minutes apart, and otherwise keeps on/off control.

Vehicle commands are billed by Tesla's Fleet API, so:

- Disable polling on the Tesla Fleet integration (**System options → Enable polling for updates**). RVik Razor refreshes the vehicles only when it needs to.
- Commands are sent only while the vehicle draws power, so it is awake, and never to a vehicle the integration believes is asleep. A wake is billed far higher than a command.
- At most 150 commands are sent per day. With real consumption this was about 25–60 a day.
- If the charger does not follow a lowered current within 2 minutes, RVik Razor assumes the wrong vehicle and keeps on/off control until it is unplugged.
- A lowered current is set back to 16 A when the vehicle is unplugged, since vehicles remember their current per location.

The projected sensor shows the status in `vehicle_trim` and the number of commands sent today in `vehicle_commands_today`.
   - Control pauses for the cycle when house power is unavailable

## Configuration Examples

### Example 1: EV Charger Only
```yaml
Loads:
  - Name: "Zaptec Charger"
    Type: EV Ampere
    Priority: 1
    Ampere Entity: number.zaptec_current
    Min Ampere: 6
    Max Ampere: 32
    Phases: 3
    Voltage: 400
    Power Sensor: sensor.zaptec_power
```

### Example 2: EV + Heat Pump with Enabled Entity
```yaml
Loads:
  - Name: "Heat Pump"
    Type: Switch
    Priority: 1 (cut first)
    Switch Entity: switch.heat_pump
    Assumed Power: 2.5 kW

  - Name: "EV Charger"
    Type: EV Ampere
    Priority: 2 (cut second)
    Ampere Entity: number.zaptec_current
    Min Ampere: 6
    Max Ampere: 16
    Enabled Entity: binary_sensor.car_charging  # Only regulate when car is charging
```

## Technical Details

### File Structure
```
custom_components/rvik_razor/
├── __init__.py          # Integration setup
├── manifest.json        # Integration metadata
├── const.py            # Constants and data models
├── config_flow.py      # UI configuration
├── coordinator.py      # Core logic and control
├── sensor.py           # Monitoring sensors
├── number.py           # Max kWh control
├── select.py           # Mode selection
└── strings.json        # UI translations
```

### Default Values
- Update interval: 30 seconds
- Cooldown: 120 seconds
- Restore margin: 0.1 kWh
- Min ampere (EV): 6A
- Max ampere (EV): 32A
- Phases (EV): 3-phase
- Voltage (EV): 400V

## Future Enhancements (Not in MVP)

- Support for total_energy sensors with baseline calculation
- Advanced power smoothing/averaging
- Configurable amp-trim limits (6–16 A, 150 commands/day are fixed)
- Per-load cooldown configuration
- Climate control integration
- Rich status entity
- Persistent state across restarts
- Energy statistics integration

## Troubleshooting

### Integration won't load
- Check that energy sensor exists and provides valid kWh values
- Verify manifest.json is valid JSON

### Loads not being controlled
- Ensure mode is set to "Control" (not Monitor or Off)
- Check that load entities are accessible
- Verify cooldown period hasn't locked out changes
- Check logs for error messages

### Oscillating behavior
- For an on/off load such as an EV charger, enable **Run late in the hour**
- Increase restore margin (currently hardcoded at 0.1 kWh)
- Verify power sensor is stable (not fluctuating wildly)
- Consider increasing update interval

### Debug Logging
Enable debug logging in configuration.yaml:
```yaml
logger:
  default: info
  logs:
    custom_components.rvik_razor: debug
```

## License

This is a custom component for Home Assistant. Use at your own risk.

## Credits

Developed by RVik for smart home energy management.
