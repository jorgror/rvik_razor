# Trim the Vehicle's Charge Current on a Late-Hour Charger

A late-hour charger load can list the charge-current entities of the vehicles that charge on it. The charger switch still follows the late-hour rule (ADR 0002). While a vehicle charges, RVik Razor also fits the vehicle's current to the room left in the hour, through the vehicle's API rather than the charger's.

- **Lower:** set the current at once to the largest whole ampere that fits with 0.2 kW to spare, down to 6 A. In the last 10 minutes of the hour, set it 1 A lower, so the current does not follow the room down one command at a time.
- **Raise:** to 1 A below what fits, only when that is at least 2 A more, has fitted for a minute, and the last change is 5 minutes old. Raising to the edge of the fit was cut back within minutes by small rises in the rest of the house.
- **Stop:** when even 6 A does not fit, the late-hour stop rule switches the charger off, judged at 6 A, before any command lowers the current. At 6 A the rule uses the charger's measured power.
- **Start:** unchanged. The charger starts only once full power fits until the end of the hour.
- **Wait:** nothing is decided for a minute after a change or a start. The Zaptec charger once reported a lower current 35 s after the meter saw it, which made the rest of the house look busier and stopped the charger.

The charger switch is free, and it is still the hard stop. Each vehicle command is a billed Fleet API request, and vehicle data requests and wakes cost more. Ten days of replayed consumption gave:

- about 63 commands a day, around $2 a month, well inside Tesla's $10 monthly credit;
- 9% more energy to the car;
- half the charger starts, about 4.6 a day;
- a worst hour of 4.79 kWh at a 4.8 kWh set point, also with injected spikes and a 60 s command delay.

The charger's own current setting was rejected. Zaptec asks for installation current updates no more than about every 15 minutes.

**Which vehicle is charging**

Two vehicles share the charger, and neither the charger nor RVik Razor knows which one is plugged in. Once the charger draws at least 1 kW, RVik Razor refreshes the listed vehicles once. It binds the single vehicle that is all of:

- online;
- charging;
- in the `home` zone;
- reporting a charger power within 1.5 kW of the charger's measured power.

No match, or more than one, leaves the load on on/off control. RVik Razor retries at most 3 times per plug-in, 5 minutes apart. After each reduction, the charger's measured power must drop within 2 minutes. If it does not, the bound vehicle is the wrong one, and trimming stops until the vehicle is unplugged.

**Consequences**

- Polling must be disabled on the vehicle integration. Its 10-minute polling of two vehicles costs more than the monthly credit.
- Commands go only to a vehicle that draws power, so it is awake, and never to one the integration believes is asleep. Commanding a sleeping vehicle would wake it first.
- At most 150 commands are sent per day. After that, the load runs on/off only until midnight.
- Vehicles remember their charge current per location. A lowered current is set back to 16 A when the vehicle is unplugged. If that command fails, the next plug-in starts at the lowered current until trim raises it.
- Trim commands do not restart the charger's minimum on and off times.
- Vehicles differ a lot in kW per ampere on the same charger, and so does one vehicle over a plug-in. On the 230 V 3-phase Zaptec charger, both Teslas drew 0.66–0.68 kW/A from plug-in until their first lower current or stop, and 0.39–0.40 kW/A after that, probably because the charger switched phases. A fixed rate made the controller pick currents and start times 70% too high, and in the replay 16 hours went over the set point. RVik Razor therefore measures the bound vehicle's rate once its current has been steady for 60 s and the vehicle has drawn power for 60 s. Measuring right after the charger started gave 0.28 kW/A for the Model 3, because it was still ramping up. At an unchanged current it keeps the highest value, since a tapering vehicle draws less than its current allows but never more. After a change it measures again. A charging stretch starts with the rate of the last one, or with what the vehicle draws when that is more. Until the first measurement, the load's assumed power is taken as full power at the maximum current, sized for the most powerful vehicle (11 kW).
- In 1.3.2 the highest rate of the plug-in was kept. After the first stop it predicted 0.66 kW/A where the vehicle drew 0.39 kW/A, so the charger started late and stopped needlessly. Replaying 2026-10-22 (two plug-ins, 09:50–19:20) through the real code, 1.3.3 gave the vehicles 17.4 kWh against 12.6 kWh, with 24 commands against 20 and 6 starts against 8. The worst hour was 4.77 kWh at the 4.8 kWh set point. With the charger reporting 35 s late or the vehicle following 30 s late, no hour went over 4.8 kWh.
- The sibling entities are found on the vehicle's device by translation key, which matches the Tesla Fleet integration.
