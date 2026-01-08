# RVik Razor - Energy Limiter for Home Assistant

RVik Razor is a custom Home Assistant integration that automatically controls your EV charger and other loads to keep your hourly energy consumption below a specified limit.

## MVP Features Implemented

### Core Functionality
- **Hourly Energy Monitoring**: Tracks current hour energy consumption from a sensor
- **Power Projection**: Estimates end-of-hour energy usage based on current power consumption
- **Automatic Load Control**: Reduces or turns off loads when approaching the hourly limit
- **Smart Restoration**: Gradually restores loads when sufficient margin exists

### Supported Load Types
1. **EV Charger (Ampere Control)**: Incrementally reduces charging amperage
2. **Switch Loads**: Turns switches on/off (e.g., heat pumps)

### Load Configuration Options
- **Priority**: Lower numbers are reduced first
- **Enabled**: Static on/off toggle for the load
- **Enabled Entity** (optional): Dynamic control via a binary sensor or input_boolean (e.g., only regulate EV charger when car is actually charging)

### Operation Modes
- **Off**: All control disabled, loads restored to original state
- **Monitor**: Only monitors and reports, no control actions
- **Control**: Active control to stay within limits

### Configuration
- **Config Flow**: Full UI-based setup
- **Options Flow**: Add/remove loads, adjust priorities and limits
- **Load Priority**: Lower numbers are reduced first
- **Cooldown**: 120-second cooldown between load changes to prevent flapping

## Setup

### 1. Initial Configuration
1. Go to Settings → Devices & Services
2. Click "Add Integration"
3. Search for "RVik Razor"
4. Select your **Energy this hour** sensor (required)
5. Select your **House power** sensor (optional but recommended)
6. Set **Max kWh per hour** limit
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
   - **Needed reduction**: How much power needs to be cut
3. Switch to **Control** mode when ready

## Entities Created

### Control Entities
- `number.rvik_razor_max_hour_kwh`: Set the hourly energy limit
- `select.rvik_razor_mode`: Change operation mode (Off/Monitor/Control)

### Monitoring Sensors
- `sensor.rvik_razor_current_hour_kwh`: Current hour energy consumption
- `sensor.rvik_razor_projected_end_hour_kwh`: Projected end-of-hour consumption
  - Attributes: remaining_seconds, house_power_kw, last_action, last_action_reason, mode
- `sensor.rvik_razor_needed_reduction`: Power reduction needed to stay within limit

## How It Works

### Estimation Logic
```
projected_end_kwh = current_hour_kwh + (house_power_kw × remaining_seconds / 3600)
needed_reduction_kw = (projected_end_kwh - max_hour_kwh) × 3600 / remaining_seconds
```

### Control Logic
1. **Reduction Phase** (when projected > limit):
   - Sort loads by priority (lowest first)
   - For each load (respecting cooldown):
     - EV: Reduce ampere by step_ampere
     - Switch: Turn off
   - Continue until needed_reduction_kw ≤ 0

2. **Restoration Phase** (when projected < limit - margin):
   - Sort loads by priority (highest first)
   - Restore one load at a time
   - Wait for next cycle before restoring more

3. **Safety Features**:
   - 120-second cooldown per load between actions
   - Stores original values for restoration
   - Hour rollover detection and reset
   - Graceful handling of unavailable sensors

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
- Min on/off time for switches
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
