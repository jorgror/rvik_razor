# Trim the Vehicle's Charge Current on a Late-Hour Charger

A late-hour charger load can list the charge-current entities of the vehicles that charge on it. The charger switch still follows the late-hour rule (ADR 0002). While a vehicle charges, RVik Razor also fits the vehicle's current to the room left in the hour, through the vehicle's API rather than the charger's.

- **Lower:** set the current at once to the largest whole ampere that fits with 0.2 kW to spare, down to 6 A. In the last 10 minutes of the hour, set it 1 A lower, so the current does not follow the room down one command at a time.
- **Raise:** to 1 A below what fits, only when that is at least 2 A more, has fitted for a minute, and the last change is 5 minutes old. Raising to the edge of the fit was cut back within minutes by small rises in the rest of the house.
- **Stop:** when even 6 A does not fit, the late-hour stop rule switches the charger off, judged at 6 A, before any command lowers the current. At 6 A the rule uses the charger's measured power.
- **Start:** at full power once it fits until the end of the hour, as in ADR 0002. A bound vehicle also restarts once 2 A above its minimum, 8 A, fits until the end of the hour. That room must fit for 3 minutes, and it must also cover 2 minutes at full power while the vehicle ramps up before its first trim. See *Restart at reduced current* below.
- **Wait:** nothing is decided for a minute after a change or a start. The Zaptec charger once reported a lower current 35 s after the meter saw it, which made the rest of the house look busier and stopped the charger.

The charger switch is free, and it is still the hard stop. Each vehicle command is a billed Fleet API request, and vehicle data requests and wakes cost more. Ten days of replayed consumption gave:

- about 63 commands a day, around $2 a month, well inside Tesla's $10 monthly credit;
- 9% more energy to the car;
- half the charger starts, about 4.6 a day;
- a worst hour of 4.79 kWh at a 4.8 kWh set point, also with injected spikes and a 60 s command delay.

The charger's own current setting was rejected. Zaptec asks for installation current updates no more than about every 15 minutes.

**Which vehicle is charging**

Two vehicles share the charger, and neither the charger nor RVik Razor knows which one is plugged in. Once the charger has drawn at least 1 kW for a minute, RVik Razor refreshes the listed vehicles once. It binds the single vehicle that is all of:

- online;
- charging;
- in the `home` zone;
- reporting a charger power within 1.5 kW of the charger's measured power.

No match, or more than one, leaves the load on on/off control. RVik Razor retries at most 3 times per plug-in, 5 minutes apart. After each reduction, the charger's measured power must drop within 2 minutes. If it does not, the bound vehicle is the wrong one, and trimming stops until the vehicle is unplugged.

Before 1.3.6 the refresh came as soon as the charger drew 1 kW. On 27 Oct 2026 the first two attempts came while the charger ramped up, at 1.2 kW and 3.7 kW against the vehicle's 6 kW, and only the last attempt of the plug-in matched.

A vehicle is unplugged when the charger switch is unavailable for two control cycles. The Zaptec integration reported nothing for 10 minutes on 27 Oct 2026, and a replug in that time never showed as an unplug. RVik Razor therefore also watches the energy sensors on the charger switch's device. The charger's session energy starts again at a new plug-in, so a drop to under half, by at least 0.5 kWh, ends the session as a replug. A lifetime energy meter never drops, and the Zaptec corrects its session energy down by 0.1–0.5 kWh when charging resumes, which is too little to count. After a replug a lowered current is not set back to 16 A, since the vehicle may already be charging again.

**Consequences**

- Polling must be disabled on the vehicle integration. Its 10-minute polling of two vehicles costs more than the monthly credit.
- Commands go only to a vehicle that draws power, so it is awake, and never to one the integration believes is asleep. Commanding a sleeping vehicle would wake it first.
- At most 150 commands are sent per day. After that, the load runs on/off only until midnight.
- Vehicles remember their charge current per location. A lowered current is set back to 16 A when the vehicle is unplugged. If that command fails, the next plug-in starts at the lowered current until trim raises it.
- Trim commands do not restart the charger's minimum on and off times.
- Vehicles differ a lot in kW per ampere on the same charger, and so does one vehicle over a plug-in. On the 230 V 3-phase Zaptec charger, both Teslas drew 0.66–0.68 kW/A from plug-in until their first lower current or stop, and 0.39–0.40 kW/A after that, probably because the charger switched phases. A fixed rate made the controller pick currents and start times 70% too high, and in the replay 16 hours went over the set point. RVik Razor therefore measures the bound vehicle's rate once its current has been steady for 60 s and the vehicle has drawn power for 60 s. Measuring right after the charger started gave 0.28 kW/A for the Model 3, because it was still ramping up. At an unchanged current it keeps the highest value, since a tapering vehicle draws less than its current allows but never more. After a change it measures again. A charging stretch starts with the rate of the last one, or with what the vehicle draws when that is more. Until the first measurement, the load's assumed power is taken as full power at the maximum current, sized for the most powerful vehicle (11 kW).
- In 1.3.2 the highest rate of the plug-in was kept. After the first stop it predicted 0.66 kW/A where the vehicle drew 0.39 kW/A, so the charger started late and stopped needlessly. Replaying 2026-10-22 (two plug-ins, 09:50–19:20) through the real code, 1.3.3 gave the vehicles 17.4 kWh against 12.6 kWh, with 24 commands against 20 and 6 starts against 8. The worst hour was 4.77 kWh at the 4.8 kWh set point. With the charger reporting 35 s late or the vehicle following 30 s late, no hour went over 4.8 kWh.
- Home Assistant only accepts a charge current within the range of the vehicle's last refresh. On 27 Oct 2026 the Tesla Fleet refresh at a restart came while the charger was paused. It read the Model Y at 5 A with a range of 0–5, although the car charged at 14 A. The next 6 A command was rejected. Before 1.3.5, any failed command meant on/off control until unplug at the assumed 11 kW, so the car only charged in the last minutes of each hour. Since 1.3.5, RVik Razor refreshes the vehicle before a command that is outside the entity's range. If the command still cannot be sent, it keeps the vehicle's measured rate for on/off control, and finds the vehicle again while it charges, within the 3 attempts per plug-in. A charger that does not follow a lowered current still ends trimming until unplug, since that vehicle may be the wrong one.
- The bound vehicle, its measured rates, a lowered current and the day's command count are saved in Home Assistant's storage and restored after a restart. Before 1.3.4 a restart lost the rate. The next start then waited for 11.6 kW of room, sized for the most powerful vehicle, where the plugged-in Model Y needed 6.1 kW. A vehicle unplugged during the restart ends the session as usual. A different vehicle plugged in during the restart is taken at the saved rate until the first reduction shows the charger does not follow.
- The sibling entities are found on the vehicle's device by translation key, which matches the Tesla Fleet integration.

**Restart at reduced current (1.3.5)**

After a stop, waiting for full power until the end of the hour left the charger off for 15–60 minutes while the rest of the house left room for 6–9 A. From 22 to 27 Oct 2026, the plugged-in hours closed 7.95 kWh short of 4.8 kWh. 7.35 kWh of that was in hours where the charger was off for more than 10 minutes. Hours trimmed throughout closed within 0.05 kWh.

A vehicle that can be trimmed stays able to absorb consumption at a reduced current, so ADR 0002's reason to wait for full power does not apply to it. It may restart once a reduced current fits. Starting as soon as 6 A fitted restarted on brief dips in the house load, and stopped again within two minutes when the load rose. A replay of 22, 26 and 27 Oct through the real control code compared the settings:

| Restart | Shortfall (kWh) | Car (kWh) | Starts | Commands | Worst hour | Worst with 3 kW at :45–:55 |
|---|---|---|---|---|---|---|
| Full power only (1.3.4) | 4.84 | 48.1 | 9 | 89 | 4.76 | 4.65 |
| 6 A fits | 2.64 | 50.2 | 19 | 101 | 4.76 | 4.95 |
| 8 A fits for 3 minutes (1.3.5) | 3.12 | 50.1 | 13 | 98 | 4.76 | 4.85 |

The replay is kinder to 1.3.4 than the real hours were, since its vehicle never tapers and the meter has no lag. The spike case is the price of charging earlier in the hour. A late spike now finds less room left, but the 0.2 kWh overshoot cap still holds.

