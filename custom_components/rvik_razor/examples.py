"""Example script demonstrating regulation logic tuning.

This script shows how to quickly test different scenarios and tune
the regulation parameters without running Home Assistant.

Run with: python -m rvik_razor.examples
Or: cd /workspaces/core/config/custom_components && python -m rvik_razor.examples
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Add parent directory to path to allow imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from rvik_razor.const import Load, LoadType
from rvik_razor.coordinator import calculate_regulation_decision


def create_ev_charger(priority: int = 50) -> Load:
    """Create an EV charger load for testing."""
    return Load(
        name="EV Charger",
        priority=priority,
        load_type=LoadType.EV_AMPERE,
        enabled=True,
        min_ampere=6,
        ampere_number_entity_id="number.ev_charger",
        assumed_power_kw=11.0,  # 3-phase 16A at 400V
    )


def create_water_heater(priority: int = 30) -> Load:
    """Create a water heater switch load for testing."""
    return Load(
        name="Water Heater",
        priority=priority,
        load_type=LoadType.SWITCH,
        enabled=True,
        switch_entity_id="switch.water_heater",
        assumed_power_kw=3.0,
    )


def create_pool_pump(priority: int = 10) -> Load:
    """Create a pool pump switch load for testing."""
    return Load(
        name="Pool Pump",
        priority=priority,
        load_type=LoadType.SWITCH,
        enabled=True,
        switch_entity_id="switch.pool_pump",
        assumed_power_kw=1.5,
    )


def simulate_hour_scenario():
    """Simulate a full hour of regulation decisions."""
    print("=" * 80)
    print("SCENARIO: Simulate approaching hourly limit")
    print("=" * 80)

    # Setup loads
    loads = [
        create_pool_pump(priority=10),  # Lowest priority - cut first
        create_water_heater(priority=30),  # Medium priority
        create_ev_charger(priority=50),  # Highest priority - cut last
    ]

    max_hour_kwh = 5.0
    restore_margin = 0.3
    current_time = time.time()

    # Simulate different points in the hour
    scenarios = [
        # (current_kwh, house_power_kw, remaining_minutes, description)
        (2.0, 4.0, 30, "Early hour, moderate usage"),
        (3.5, 6.0, 20, "Mid-hour, high usage - approaching limit"),
        (4.2, 8.0, 15, "Late hour, very high usage - exceeding limit"),
        (4.5, 3.0, 10, "Late hour, usage reduced"),
        (4.6, 2.0, 5, "Near end, low usage - can restore"),
    ]

    for current_kwh, house_power_kw, remaining_mins, description in scenarios:
        print(f"\n{'─' * 80}")
        print(f"⏰ {60 - remaining_mins} minutes into hour: {description}")
        print(f"{'─' * 80}")

        # Calculate projection
        remaining_seconds = remaining_mins * 60
        projected_end_kwh = current_kwh + (house_power_kw * remaining_seconds / 3600.0)
        needed_reduction_kw = max(
            0.0, (projected_end_kwh - max_hour_kwh) * 3600.0 / remaining_seconds
        )

        print(
            f"📊 Current: {current_kwh:.2f} kWh | House power: {house_power_kw:.2f} kW"
        )
        print(
            f"📈 Projected end: {projected_end_kwh:.2f} kWh | Max: {max_hour_kwh:.2f} kWh"
        )

        # Get decision
        decision = calculate_regulation_decision(
            loads=loads,
            needed_reduction_kw=needed_reduction_kw,
            projected_end_kwh=projected_end_kwh,
            max_hour_kwh=max_hour_kwh,
            current_time=current_time,
            restore_margin=restore_margin,
            cooldown=60,  # Short cooldown for simulation
        )

        # Display decision
        print(f"\n🤖 Decision: {decision['action'].upper()}")
        print(f"💬 Reason: {decision['reason']}")

        if decision["loads_to_reduce"]:
            print(f"⬇️  Reducing {len(decision['loads_to_reduce'])} load(s):")
            for plan in decision["loads_to_reduce"]:
                load = plan["load"]
                print(f"   - {load.name} (priority {load.priority})")
                # Simulate the reduction
                load.original_value = "on"  # Mark as reduced
                load.last_action_time = current_time

        if decision["loads_to_restore"]:
            print(f"⬆️  Restoring {len(decision['loads_to_restore'])} load(s):")
            for load in decision["loads_to_restore"]:
                print(f"   - {load.name} (priority {load.priority})")
                # Simulate the restoration
                load.original_value = None
                load.last_action_time = current_time

        # Advance time for next iteration
        current_time += 60  # 1 minute


def test_priority_ordering():
    """Test that loads are reduced in correct priority order."""
    print("\n" + "=" * 80)
    print("TEST: Priority ordering")
    print("=" * 80)

    loads = [
        create_pool_pump(priority=10),
        create_water_heater(priority=30),
        create_ev_charger(priority=50),
    ]

    decision = calculate_regulation_decision(
        loads=loads,
        needed_reduction_kw=5.0,  # Need to reduce multiple loads
        projected_end_kwh=7.0,
        max_hour_kwh=5.0,
        current_time=time.time(),
    )

    print(f"\nNeeded reduction: 5.0 kW")
    print(f"Decision: {decision['action']}")
    print(f"\nReduction order:")
    for i, plan in enumerate(decision["loads_to_reduce"], 1):
        load = plan["load"]
        print(f"{i}. {load.name} (priority {load.priority})")

    # Verify order
    priorities = [p["load"].priority for p in decision["loads_to_reduce"]]
    if priorities == sorted(priorities):
        print("\n✅ Loads are correctly ordered by priority (lowest first)")
    else:
        print("\n❌ ERROR: Loads are NOT in correct priority order!")


def test_cooldown_behavior():
    """Test cooldown prevention."""
    print("\n" + "=" * 80)
    print("TEST: Cooldown behavior")
    print("=" * 80)

    current_time = time.time()
    cooldown = 300  # 5 minutes

    loads = [
        create_pool_pump(priority=10),
        create_water_heater(priority=30),
    ]

    # Set one load as recently acted upon
    loads[0].last_action_time = current_time - 60  # 1 minute ago
    loads[1].last_action_time = current_time - 400  # 6+ minutes ago

    decision = calculate_regulation_decision(
        loads=loads,
        needed_reduction_kw=2.0,
        projected_end_kwh=6.0,
        max_hour_kwh=5.0,
        current_time=current_time,
        cooldown=cooldown,
    )

    print(f"\nCooldown period: {cooldown} seconds")
    print(f"{loads[0].name}: Last action 60 seconds ago (in cooldown)")
    print(f"{loads[1].name}: Last action 400 seconds ago (available)")
    print(f"\nDecision: {decision['action']}")

    if decision["loads_to_reduce"]:
        selected = decision["loads_to_reduce"][0]["load"]
        print(f"Selected load: {selected.name}")
        if selected.name == loads[1].name:
            print("✅ Correctly skipped load in cooldown")
        else:
            print("❌ ERROR: Selected load that's in cooldown!")
    else:
        print("❌ ERROR: No loads selected!")


def test_restore_margin():
    """Test restore margin behavior."""
    print("\n" + "=" * 80)
    print("TEST: Restore margin")
    print("=" * 80)

    load = create_pool_pump()
    load.original_value = "on"  # Previously reduced

    max_hour_kwh = 5.0
    restore_margin = 0.5

    test_cases = [
        (4.9, "Too close to limit"),
        (4.5, "At exact margin boundary"),
        (4.4, "Below margin - should restore"),
    ]

    for projected_kwh, description in test_cases:
        decision = calculate_regulation_decision(
            loads=[load],
            needed_reduction_kw=0.0,
            projected_end_kwh=projected_kwh,
            max_hour_kwh=max_hour_kwh,
            current_time=time.time(),
            restore_margin=restore_margin,
        )

        margin = max_hour_kwh - projected_kwh
        print(f"\nProjected: {projected_kwh:.2f} kWh (margin: {margin:.2f} kWh)")
        print(f"Description: {description}")
        print(f"Action: {decision['action']}")
        print(f"Reason: {decision['reason']}")


if __name__ == "__main__":
    print("\n🔧 Rvik Razor Regulation Logic Examples\n")

    # Run examples
    simulate_hour_scenario()
    test_priority_ordering()
    test_cooldown_behavior()
    test_restore_margin()

    print("\n" + "=" * 80)
    print("✅ All examples completed!")
    print("=" * 80)
    print("\nTo modify the logic:")
    print("1. Edit calculate_regulation_decision() in coordinator.py")
    print("2. Run: python test_regulation.py -v")
    print("3. Run: python examples.py")
    print("4. Iterate until behavior is correct")
    print()
