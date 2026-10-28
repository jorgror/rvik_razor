# Cut Switch Loads Only When the Hour Needs It

Switch loads under set-point control, such as a water heater or a heat pump, are now kept on unless the hour cannot be held without them. This replaces ADR 0001's accuracy rule for switch loads. Ampere loads still follow ADR 0001.

- **Cut** the first load by priority when the hour is projected over the set point and, without that load, would still end within 0.1 kWh of it. Cutting later still holds the hour if the rest of the house stays where it is, and avoids the cut if it falls back.
- **Never cut** a load drawing less than 0.1 kW. A heater whose thermostat is satisfied saves nothing by being cut.
- **Restore** the first load by priority, highest number first, once the hour is projected 0.2 kWh under the set point without it.
- **Projection:** the rest of the house is everything except the late-hour loads and the switch loads. A late-hour load follows its own rule (ADR 0002), which stops it first. The rest of the house is projected both at its current power and at its average over the last 5 minutes. A cut uses the lower of the two, and a restore the higher. A brief spike leaves the average low, and a load that has just stopped leaves the current power low.

ADR 0001 projected the whole house at its current power and cut a switch load as soon as that made the hour more accurate. A brief spike early in the hour projected far over the set point. From 22 to 27 Oct 2026 the water heater was cut 10 times, all between :01 and :25. Every one of those hours closed 0.2–3.3 kWh under 4.8 kWh. Half of the cuts came while the heater drew nothing. The running EV's power also counted in the projection, so a charger stopping at :00 could cut the heater at :01.

A replay of 22–27 Oct through the real decision code, with the water heater simulated:

| Case | ADR 0001 cuts | Heater off (min) | Worst hour | This rule's cuts | Heater off (min) | Worst hour |
|---|---|---|---|---|---|---|
| As recorded | 25 | 150 | 4.81 | 2 | 10 | 4.81 |
| No EV, +3 kW all hour | 41 | 3020 | 6.68 | 30 | 1994 | 6.68 |
| No EV, +4 kW at :00–:30 | 106 | 2759 | 5.68 | 34 | 1021 | 5.68 |
| No EV, +5 kW at :40–:60 | 22 | 242 | 5.34 | 5 | 65 | 5.36 |

In the stress cases, a 2 kW heater cannot hold the hour under either rule. This rule's worst hours stay within 0.05 kWh of ADR 0001's.

**Consequences**

- Switch loads now use the room in the hour before a late-hour load does. A late-hour EV starts later or at a lower current when a heater runs.
- Priority still orders switch loads among themselves. Ampere loads are decided after the switch loads, by ADR 0001.
- A load that persists from early in the hour is cut a few minutes later than before, once the 5-minute average has caught up with it.
- The per-load timeout still applies between actions. A heat pump should have a longer one, for example 15–20 minutes, so its compressor is not cycled.
