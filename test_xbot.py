#!/usr/bin/env python3
"""Test runner for XBot system."""

import sys
import asyncio
import logging
from pathlib import Path

# Add xbot to path
sys.path.insert(0, str(Path(__file__).parent))

from xbot.app.config import CLIConfig
from xbot.app.main import run_with_config


async def run_smoke_tests():
    """Run comprehensive smoke tests."""
    print("=== XBot Smoke Test Suite ===\n")

    # Test configurations
    test_configs = [
        {
            "name": "Mock Tracking Limit (Buy)",
            "config": CLIConfig(
                venue="mock",
                symbol="BTC",
                mode="tracking_limit",
                side="buy",
                timeout_secs=30.0,
                debug=True
            )
        },
        {
            "name": "Mock Tracking Limit (Sell)",
            "config": CLIConfig(
                venue="mock",
                symbol="ETH",
                mode="tracking_limit",
                side="sell",
                timeout_secs=30.0,
                debug=True
            )
        },
        {
            "name": "Mock Limit Once",
            "config": CLIConfig(
                venue="mock",
                symbol="SOL",
                mode="limit_once",
                side="buy",
                timeout_secs=20.0,
                debug=True
            )
        },
        {
            "name": "Mock Market Order",
            "config": CLIConfig(
                venue="mock",
                symbol="BTC",
                mode="market",
                side="sell",
                timeout_secs=15.0,
                debug=True
            )
        }
    ]

    results = []
    for i, test in enumerate(test_configs, 1):
        print(f"[{i}/{len(test_configs)}] Running: {test['name']}")
        print("-" * 50)

        try:
            # Suppress logs for cleaner output
            logging.getLogger().setLevel(logging.ERROR)

            # Run test
            system_config = test["config"].to_system_config()
            exit_code = await run_with_config(system_config)

            success = exit_code == 0
            results.append({
                "name": test["name"],
                "success": success,
                "exit_code": exit_code
            })

            status = "✅ PASSED" if success else "❌ FAILED"
            print(f"Result: {status} (exit code: {exit_code})")

        except Exception as e:
            results.append({
                "name": test["name"],
                "success": False,
                "error": str(e)
            })
            print(f"Result: ❌ FAILED (error: {e})")

        print()

    # Summary
    print("=== Test Summary ===")
    passed = sum(1 for r in results if r["success"])
    total = len(results)

    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total - passed}/{total}")

    if passed == total:
        print("\n🎉 All tests passed!")
        return 0
    else:
        print(f"\n💥 {total - passed} test(s) failed:")
        for result in results:
            if not result["success"]:
                error_msg = result.get('error', f"exit code {result.get('exit_code', '?')}")
                error_info = f" ({error_msg})"
                print(f"  - {result['name']}{error_info}")
        return 1


async def run_quick_test():
    """Run a single quick test."""
    print("=== XBot Quick Test ===\n")

    config = CLIConfig(
        venue="mock",
        symbol="BTC",
        mode="tracking_limit",
        side="buy",
        timeout_secs=15.0,
        debug=False
    )

    # Suppress detailed logs
    logging.getLogger().setLevel(logging.WARNING)

    system_config = config.to_system_config()
    exit_code = await run_with_config(system_config)

    if exit_code == 0:
        print("✅ Quick test PASSED")
    else:
        print(f"❌ Quick test FAILED (exit code: {exit_code})")

    return exit_code


async def main():
    """Main test runner."""
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        return await run_quick_test()
    else:
        return await run_smoke_tests()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))