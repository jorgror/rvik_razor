# Run Binary Loads Late in the Hour

A switch load can be marked as a **late-hour load**. RVik Razor then leaves it out of horizon set-point control and applies its own rule:

- **Start:** start the load only once it can run at full power until the end of the hour, with at least 0.6 kW to spare.
- **Stop:** stop the load once the rest of the house no longer leaves room for it. A shortfall of up to 1.0 kW is accepted.
- **Overshoot cap:** while the load runs, the projection may never exceed the set point by more than 0.2 kWh.
- **Minimum times:** the load stays on and off for at least 5 minutes each. The overshoot cap overrides the minimum on time.

We chose this because horizon control made an on/off EV charger dither around the set point. A 4 kW binary load can only land the hour accurately by switching back and forth, and the switching got faster near the end of each hour. Replaying ten days of real consumption gave about 38 charger starts a day.

Charging late also keeps the hour safe. Spending the budget early leaves nothing to cut when a shower or cooking starts late in the hour. With injected 2.5–4 kW spikes, both horizon control and an "early headroom" rule exceeded 5 kWh. The late-hour rule did not.

**Consequences**

- Late-hour loads start at most about once per hour. The replay gave about 13 starts a day.
- A binary load can only start when a full-power run to the end of the hour fits, so headroom left over at the end of the hour goes unused. In the replay, the car received about 17% less energy per plugged-in hour.
- Ampere control of the charger would recover most of that energy, and is the natural next step.
- The configured per-load timeout does not apply to late-hour loads; the fixed minimum on and off times replace it.
- Horizon control (ADR 0001) still governs all other loads, around whatever the late-hour loads are doing.
