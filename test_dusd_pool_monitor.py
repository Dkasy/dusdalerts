import importlib
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from hexbytes import HexBytes


# The monitor validates required settings at import time. Tests use inert values
# and replace every external side effect with a mock.
os.environ.setdefault("BSC_RPC_URL", "http://127.0.0.1:1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
os.environ.setdefault("SEND_STARTUP_MESSAGE", "false")

monitor = importlib.import_module("dusd_pool_monitor")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.old_state = monitor.state
        monitor.state = monitor.DEFAULT_STATE.copy()

        # Actual ordering and decimals of the configured on-chain pool.
        monitor.token0 = monitor.USDT_ADDRESS
        monitor.token1 = monitor.DUSD_ADDRESS
        monitor.decimals0 = 18
        monitor.decimals1 = 6
        monitor.dusd_decimals = 6
        monitor.usdt_decimals = 18

    def tearDown(self):
        monitor.state = self.old_state

    def test_price_conversion_for_real_pool_order(self):
        # At parity, the raw DUSD/USDT base-unit ratio is 10**-12.
        sqrt_price_x96 = (2**96) // 10**6
        price = monitor.dusd_price_from_sqrt(sqrt_price_x96)
        self.assertLess(abs(price - Decimal("1")), Decimal("0.000000000001"))

    def test_signed_swap_amounts_and_direction_inputs(self):
        dusd_delta, usdt_delta = monitor.split_amounts(
            -(50_000 * 10**18),
            49_900 * 10**6,
        )
        self.assertEqual(dusd_delta, Decimal("49900"))
        self.assertEqual(usdt_delta, Decimal("-50000"))

    def test_large_swap_alert_has_valid_transaction_link(self):
        event = {
            "transactionHash": HexBytes("0x" + "ab" * 32),
            "args": {
                "amount0": -(60_000 * 10**18),
                "amount1": 59_900 * 10**6,
                "sqrtPriceX96": (2**96) // 10**6,
                "sender": "0x" + "12" * 20,
            },
        }

        with patch.object(monitor, "send_telegram") as send, patch.object(
            monitor, "save_state"
        ):
            monitor.handle_swap(event)

        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("LARGE DUSD POOL SWAP", message)
        self.assertIn("https://bscscan.com/tx/0x" + "ab" * 32, message)

    def test_telegram_errors_do_not_leak_bot_token(self):
        leaked_error = RuntimeError(
            "request failed for https://api.telegram.org/bot"
            + monitor.TELEGRAM_BOT_TOKEN
            + "/sendMessage"
        )
        with patch.object(
            monitor.requests, "post", side_effect=leaked_error
        ), patch.object(monitor.time, "sleep"), self.assertLogs(
            "dusd-monitor", level="WARNING"
        ) as captured:
            with self.assertRaises(RuntimeError) as raised:
                monitor.send_telegram("test")

        combined = "\n".join(captured.output) + str(raised.exception)
        self.assertNotIn(monitor.TELEGRAM_BOT_TOKEN, combined)
        self.assertIn("<redacted>", combined)

    def test_depeg_alert_hysteresis(self):
        with patch.object(monitor, "send_telegram") as send, patch.object(
            monitor, "save_state"
        ):
            monitor.check_depeg(Decimal("0.997"))
            monitor.check_depeg(Decimal("0.996"))
            self.assertEqual(send.call_count, 1)
            self.assertTrue(monitor.state["depeg_active"])

            monitor.check_depeg(Decimal("0.999"))
            self.assertFalse(monitor.state["depeg_active"])

            monitor.check_depeg(Decimal("0.997"))
            self.assertEqual(send.call_count, 2)

    def test_tvl_drop_alert_resets_reference(self):
        monitor.state["reference_tvl_usd"] = "1000000"
        snapshot = {
            "price": Decimal("1"),
            "dusd_balance": Decimal("450000"),
            "usdt_balance": Decimal("449999"),
            "nominal_tvl": Decimal("899999"),
            "marked_tvl": Decimal("899999"),
        }

        with patch.object(monitor, "send_telegram") as send, patch.object(
            monitor, "save_state"
        ):
            monitor.check_tvl(snapshot)

        send.assert_called_once()
        self.assertEqual(monitor.state["reference_tvl_usd"], "899999")

    def test_event_progress_is_saved_per_complete_block(self):
        first = {
            "blockNumber": 10,
            "logIndex": 0,
            "topics": [HexBytes(monitor.SWAP_TOPIC)],
        }
        second = {
            "blockNumber": 11,
            "logIndex": 0,
            "topics": [HexBytes(monitor.SWAP_TOPIC)],
        }
        decoder = Mock()
        decoder.process_log.side_effect = lambda entry: entry
        fake_pool = Mock()
        fake_pool.events.Swap.return_value = decoder
        monitor.state["last_block"] = 9

        def fail_on_second(event):
            if event["blockNumber"] == 11:
                raise RuntimeError("simulated delivery failure")

        with patch.object(
            monitor, "fetch_relevant_logs", return_value=[second, first]
        ), patch.object(monitor, "pool", fake_pool), patch.object(
            monitor, "handle_swap", side_effect=fail_on_second
        ), patch.object(monitor, "save_state") as save:
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                monitor.process_block_range(10, 12)

        self.assertEqual(monitor.state["last_block"], 10)
        save.assert_called_once()

    def test_snapshot_calls_are_pinned_to_confirmed_block(self):
        slot_call = Mock()
        slot_call.call.return_value = [(2**96) // 10**6]
        dusd_call = Mock()
        dusd_call.call.return_value = 500_000 * 10**6
        usdt_call = Mock()
        usdt_call.call.return_value = 500_000 * 10**18

        fake_pool = Mock()
        fake_pool.functions.slot0.return_value = slot_call
        fake_dusd = Mock()
        fake_dusd.functions.balanceOf.return_value = dusd_call
        fake_usdt = Mock()
        fake_usdt.functions.balanceOf.return_value = usdt_call

        with patch.object(monitor, "pool", fake_pool), patch.object(
            monitor, "dusd", fake_dusd
        ), patch.object(monitor, "usdt", fake_usdt):
            snapshot = monitor.current_snapshot(12345)

        slot_call.call.assert_called_once_with(block_identifier=12345)
        dusd_call.call.assert_called_once_with(block_identifier=12345)
        usdt_call.call.assert_called_once_with(block_identifier=12345)
        self.assertLess(
            abs(snapshot["nominal_tvl"] - Decimal("1000000")),
            Decimal("0.000001"),
        )

    def test_corrupt_state_values_are_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"last_block": -1, "reference_tvl_usd": "broken", '
                '"depeg_active": "false"}'
            )
            with patch.object(monitor, "STATE_FILE", path):
                loaded = monitor.load_state()

        self.assertIsNone(loaded["last_block"])
        self.assertIsNone(loaded["reference_tvl_usd"])
        self.assertFalse(loaded["depeg_active"])


if __name__ == "__main__":
    unittest.main()
