# Trim the Vehicle's Charge Current on a Late-Hour Charger

A late-hour charger load can list the charge-current entities of the vehicles that charge on it. The charger switch still follows the late-hour rule (ADR 0002). While a vehicle charges, RVik Razor also fits the vehicle's current to the room left in the hour, through the vehicle's API rather than the charger's.

- **Lower:** set the current at once to the largest whole ampere that fits with 0.2 kW to spare, down to 6 A.
- **Raise:** only when at least 2 A more fits and the last change is 5 minutes old.
- **Stop:** when even 6 A does not fit, the late-hour stop rule switches the charger off, judged at 6 A.
- **Start:** unchanged. The charger starts only once full power fits until the end of the hour.

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
- The sibling entities are found on the vehicle's device by translation key, which matches the Tesla Fleet integration.
