import importlib
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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

    def test_example_standx_withdraw_alert(self):
        event = {
            "transactionHash": HexBytes(
                "0x9a622d7b10a6c240ba74b096ac7f5ae92794330809390d987658a3c8f5a03fdd"
            ),
            "args": {
                "from": monitor.STANDX_HIGHWAY_ADDRESS,
                "to": "0x0808A2B6962EF20936431178743E47277016104d",
                "value": 148_698_502_775,
            },
        }

        with patch.object(monitor, "send_telegram") as send:
            monitor.handle_standx_withdraw(event)

        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("STANDX WITHDRAWAL COMPLETED", message)
        self.assertIn("148,698.502775 DUSD", message)
        self.assertIn(event["args"]["to"], message)
        self.assertIn(
            "https://bscscan.com/tx/" + event["transactionHash"].hex(), message
        )

    def test_small_standx_withdraw_also_alerts(self):
        event = {
            "transactionHash": HexBytes("0x" + "cd" * 32),
            "args": {
                "from": monitor.STANDX_HIGHWAY_ADDRESS,
                "to": "0x" + "34" * 20,
                "value": 1_000 * 10**6,
            },
        }

        with patch.object(monitor, "send_telegram") as send:
            monitor.handle_standx_withdraw(event)

        send.assert_called_once()
        self.assertIn("1,000.000000 DUSD", send.call_args.args[0])

    def test_real_standx_redeem_request_shape_alerts(self):
        event = {
            "transactionHash": HexBytes(
                "0x1482d1afd3dceeba69f165dd1178f0fccb5472779e476fd85bcb24d62b2e7cce"
            ),
            "args": {
                "user": "0xD7e526459F82bb3b43DCC73e25BD3AfAaA4ad637",
                "amount": 20_095_363_765,
                "id": 0,
            },
        }

        with patch.object(monitor, "send_telegram") as send:
            monitor.handle_standx_redeem_request(event)

        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("STANDX REDEMPTION REQUESTED", message)
        self.assertIn("20,095.363765 DUSD", message)
        self.assertIn(event["args"]["user"], message)
        self.assertIn("Redemption ID: <code>0</code>", message)

    def test_real_standx_redeem_completion_shape_alerts(self):
        event = {
            "transactionHash": HexBytes(
                "0x774b31976c933824dfb46fda00d0908064cf130eacde5c2fdd10c27c52657320"
            ),
            "args": {
                "user": "0xD3dAC35eEeA16A40715A17Ae67C30e96B6508BC6",
                "amount": 998_500_500_000_000_000_000,
                "id": 0,
            },
        }

        with patch.object(monitor, "send_telegram") as send:
            monitor.handle_standx_redeem(event)

        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("STANDX REDEMPTION COMPLETED", message)
        self.assertIn("998.500500 USDT/USDC", message)
        self.assertIn(event["args"]["user"], message)
        self.assertIn("Redemption ID: <code>0</code>", message)

    def test_fetches_standx_withdraws_from_dusd_highway_transfers(self):
        with patch.object(
            monitor.w3.eth, "get_logs", side_effect=[[], []]
        ) as get_logs:
            monitor.fetch_relevant_logs(100, 200)

        self.assertEqual(get_logs.call_count, 2)
        protocol_filter = get_logs.call_args_list[0].args[0]
        self.assertEqual(
            protocol_filter["address"],
            [monitor.POOL_ADDRESS, monitor.STANDX_GATEWAY_ADDRESS],
        )
        self.assertEqual(
            protocol_filter["topics"],
            [[
                monitor.SWAP_TOPIC,
                monitor.BURN_TOPIC,
                monitor.WITHDRAW_REQUEST_TOPIC,
                monitor.WITHDRAW_TOPIC,
            ]],
        )
        withdraw_filter = get_logs.call_args_list[1].args[0]
        self.assertEqual(withdraw_filter["address"], monitor.DUSD_ADDRESS)
        self.assertEqual(
            withdraw_filter["topics"],
            [monitor.TRANSFER_TOPIC, monitor.STANDX_HIGHWAY_TOPIC],
        )
        for event_filter in (protocol_filter, withdraw_filter):
            self.assertEqual(event_filter["fromBlock"], 100)
            self.assertEqual(event_filter["toBlock"], 200)

    def test_real_log_shapes_decode_and_dispatch_all_standx_event_types(self):
        def log_entry(
            *, address, topics, data, block_number, log_index, tx_hash
        ):
            return {
                "address": address,
                "topics": [HexBytes(topic) for topic in topics],
                "data": HexBytes(data),
                "blockNumber": block_number,
                "transactionHash": HexBytes(tx_hash),
                "transactionIndex": 0,
                "blockHash": HexBytes("0x" + f"{block_number:064x}"),
                "logIndex": log_index,
                "removed": False,
            }

        highway_withdraw = log_entry(
            address=monitor.DUSD_ADDRESS,
            topics=[
                monitor.TRANSFER_TOPIC,
                monitor.STANDX_HIGHWAY_TOPIC,
                monitor.address_topic(
                    "0x0808A2B6962EF20936431178743E47277016104d"
                ),
            ],
            data="0x" + f"{148_698_502_775:064x}",
            block_number=9,
            log_index=0,
            tx_hash=(
                "0x9a622d7b10a6c240ba74b096ac7f5ae92794330809390d987658a3c8f5a03fdd"
            ),
        )
        redeem_request = log_entry(
            address=monitor.STANDX_GATEWAY_ADDRESS,
            topics=[
                monitor.WITHDRAW_REQUEST_TOPIC,
                monitor.address_topic(
                    "0xD7e526459F82bb3b43DCC73e25BD3AfAaA4ad637"
                ),
            ],
            data=(
                "0x"
                + f"{20_095_363_765:064x}"
                + f"{0:064x}"
            ),
            block_number=10,
            log_index=0,
            tx_hash=(
                "0x1482d1afd3dceeba69f165dd1178f0fccb5472779e476fd85bcb24d62b2e7cce"
            ),
        )
        redeem_completion = log_entry(
            address=monitor.STANDX_GATEWAY_ADDRESS,
            topics=[
                monitor.WITHDRAW_TOPIC,
                monitor.address_topic(
                    "0xD3dAC35eEeA16A40715A17Ae67C30e96B6508BC6"
                ),
            ],
            data=(
                "0x"
                + f"{998_500_500_000_000_000_000:064x}"
                + f"{0:064x}"
            ),
            block_number=11,
            log_index=0,
            tx_hash=(
                "0x774b31976c933824dfb46fda00d0908064cf130eacde5c2fdd10c27c52657320"
            ),
        )

        with patch.object(
            monitor,
            "fetch_relevant_logs",
            return_value=[redeem_completion, highway_withdraw, redeem_request],
        ), patch.object(monitor, "send_telegram") as send, patch.object(
            monitor, "save_state"
        ):
            monitor.process_block_range(9, 11)

        self.assertEqual(send.call_count, 3)
        messages = "\n".join(call.args[0] for call in send.call_args_list)
        self.assertIn("STANDX WITHDRAWAL COMPLETED", messages)
        self.assertIn("STANDX REDEMPTION REQUESTED", messages)
        self.assertIn("STANDX REDEMPTION COMPLETED", messages)
        self.assertEqual(monitor.state["last_block"], 11)

    def test_unlimited_backfill_preserves_old_cursor(self):
        monitor.state["last_block"] = 1
        snapshot = {
            "price": Decimal("1"),
            "dusd_balance": Decimal("500000"),
            "usdt_balance": Decimal("500000"),
            "nominal_tvl": Decimal("1000000"),
            "marked_tvl": Decimal("1000000"),
        }
        fake_w3 = SimpleNamespace(eth=SimpleNamespace(block_number=20_000))

        with patch.object(monitor, "MAX_BACKFILL_BLOCKS", 0), patch.object(
            monitor, "w3", fake_w3
        ), patch.object(
            monitor, "current_snapshot", return_value=snapshot
        ), patch.object(monitor, "check_depeg"), patch.object(
            monitor, "check_tvl"
        ), patch.object(monitor, "save_state"):
            monitor.initialize_state()

        self.assertEqual(monitor.state["last_block"], 1)

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
