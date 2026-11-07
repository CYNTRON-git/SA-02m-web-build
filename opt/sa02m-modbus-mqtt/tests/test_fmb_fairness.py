"""Equal-priority WB arbitration across one busy line, including real poll ACKs."""
from collections import Counter, deque
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge_fmb import FastModbusEventPortManager
from bridge_serial import ScanHold
from test_fmb_mr_reply_truth import SerialReplies, with_crc


class Line:
    def __init__(self, queues):
        self.queues = {addr: deque(values) for addr, values in queues.items()}
        self.flags = dict.fromkeys(queues, 0x55)
        self.in_flight = set()
        self.requests = []
        self.last_reply = (0, 0)
        self.confirmed = Counter()
        self.fault = None

    def accepts_ack(self, addr, minimum):
        return True

    def remove_confirmed(self, addr):
        self.queues[addr].popleft()

    def fmb_send_recv(self, frame, *args):
        if with_crc(frame[:-2]) != frame or frame[:3] != b'\xfd\x46\x10':
            raise AssertionError('Invalid WB poll')
        minimum, _, ack, flag = frame[3:7]
        if self.fault is ScanHold:
            self.fault = None
            raise ScanHold()
        if (ack, flag) != self.last_reply:
            raise AssertionError('Master did not acknowledge the last complete DATA')
        self.requests.append(minimum)
        if ack in self.in_flight and self.flags[ack] == flag and self.accepts_ack(ack, minimum):
            self.remove_confirmed(ack)
            self.confirmed[ack] += 1
            self.in_flight.remove(ack)
            self.flags[ack] ^= 0xff
        if self.fault is not None:
            response, self.fault = self.fault, None
            return response
        eligible = [addr for addr in self.queues if addr >= minimum]
        if not eligible:
            return b''
        busy = [addr for addr in eligible if self.queues[addr]]
        addr = min(busy or eligible)
        if not busy:
            return with_crc(bytes([addr, 0x46, 0x12, self.flags[addr]]))
        self.last_reply = (addr, self.flags[addr])
        self.in_flight.add(addr)
        record = b'\x02\x04\x00\x69' + self.queues[addr][0].to_bytes(2, 'little')
        return with_crc(bytes([addr, 0x46, 0x11, self.flags[addr], 1, len(record)]) + record)


class WinnerAckLine(Line):
    """Older slaves examine the confirmation only after winning arbitration."""

    def __init__(self, queues, *, early=(), stuck=()):
        super().__init__(queues)
        self.early = set(early)
        self.stuck = set(stuck)

    def accepts_ack(self, addr, minimum):
        busy = [a for a, q in self.queues.items() if a >= minimum and q]
        return addr not in self.stuck and (addr in self.early or addr == min(busy, default=None))


class OverflowLine(WinnerAckLine):
    def fmb_send_recv(self, frame, *args):
        # New samples evict the entire one-record head before every confirm.
        # The device retains its in-flight flag until a matching ACK wins.
        for addr in self.in_flight:
            self.queues[addr].popleft()
        return super().fmb_send_recv(frame, *args)

    def remove_confirmed(self, addr):
        pass  # The queue no longer contains any records from that transmission.


class Waiter:
    def fmb_needs_own_empty(self):
        return True


def manager(addresses, waiters=()):
    result = FastModbusEventPortManager('test', 19200)
    for addr in addresses:
        result.register_device(addr, str(addr), [(3, 105, 1)], lambda *args: None,
                               poller=Waiter() if addr in waiters else None)
        result._devices[addr]['configured'] = True
    return result


class TestBusFairness(unittest.TestCase):
    def test_msw_lux_wide_value_does_not_block_single_word_in_same_packet(self):
        # WB-MSW v3 lux uses four bytes starting at IR9. SA's existing
        # device callbacks accept one scalar register per configured point.
        wide = bytes.fromhex('04 04 00 09 78 56 34 12')
        word = bytes.fromhex('02 04 00 09 cd ab')
        report = with_crc(bytes.fromhex('05 46 11 00 03 0e') + wide + word)
        bus = SerialReplies([report, bytes.fromhex('fd4612525d')])
        mgr = manager([5])
        with mock.patch('bridge_fmb._get_port', return_value=bus), \
                mock.patch.object(mgr, '_dispatch') as deliver:
            self.assertEqual(mgr.event_burst(), 0)
        self.assertEqual([c.args for c in deliver.call_args_list], [(5, 3, 9, 0xabcd)])
        self.assertEqual(bus.sent[1][0][5:7], bytes((5, 0)))

    def test_wb_queue_count_and_arbitration_prefix_to_last_ack(self):
        report = bytes.fromhex('05 46 11 01 02 06 02 04 01 D0 04 00 6B B9')
        count = bytes.fromhex('05 46 11 01 01 00 DC ED')
        end = bytes.fromhex('FD 46 12 52 5D')
        prefix = bytes.fromhex('0A 46 11 00')
        mgr = manager([5])
        bus = SerialReplies([bytes.fromhex('00 46 11 ff') + end, prefix + end, prefix + report, count, end])
        for minimum, budget, events, ack in (
                (1, 6, [], (0, 0)), (1, 6, [], (0, 0)),
                (1, 6, [(5, 3, 464, 4)], (0, 0)),
                (1, 4, [], (5, 1)), (5, 6, [], (5, 1))):
            mgr.MAX_DATA_LEN = budget
            self.assertEqual(mgr._poll_once(bus)[1], events)
            self.assertEqual(bus.sent[-1][0], with_crc(bytes((0xfd, 0x46, 0x10, minimum, budget, *ack))))
            self.assertTrue(mgr._last_poll_answered)
            self.assertIsNone(mgr._poll_empty_slave)

    def test_count_only_replies_end_the_burst_and_preserve_record_generation(self):
        for flag in (0, 1, 0x55, 0xaa):
            with self.subTest(flag=flag):
                count = with_crc(bytes((5, 0x46, 0x11, flag, 1, 0)))
                mgr = manager([5])
                bus = SerialReplies([count, b''])
                mgr.MAX_DATA_LEN = 4
                with mock.patch('bridge_fmb._get_port', return_value=bus), \
                        mock.patch.object(mgr, '_dispatch') as deliver:
                    self.assertEqual(mgr.event_burst(), 0)
                self.assertEqual(len(bus.sent), 1)
                deliver.assert_not_called()
                self.assertIsNone(mgr._poll_empty_slave)
                self.assertNotIn(5, mgr._wb_frame_slaves)
                mgr._poll_once(bus)
                self.assertEqual(bus.sent[-1][0], with_crc(bytes((0xfd, 0x46, 0x10, 1, 4, 5, flag))))

    def test_count_only_cannot_clear_larger_possible_inflight_batch(self):
        mgr = manager([5])
        count = bytes.fromhex('05 46 11 01 01 00 DC ED')
        end = with_crc(bytes.fromhex('05461201'))
        bus = SerialReplies([b'', count, end, end])
        for budget, own in ((128, None), (4, None), (6, None), (128, 5)):
            mgr.MAX_DATA_LEN = budget
            mgr._poll_once(bus)
            self.assertEqual(mgr._poll_empty_slave, own)
        self.assertEqual(bus.sent[-1][0][5:7], bytes((5, 1)))

    def test_count_only_winner_shares_polls_with_higher_addresses(self):
        mgr = manager([5, 9, 12])
        count = bytes.fromhex('05 46 11 01 01 00 DC ED')
        def answer(request, *_):
            minimum = request[3]
            if minimum <= 5:
                return count
            return with_crc(bytes((minimum, 0x46, 0x11, 0, 1, 6, 2, 4, 1, 0xd0, 4, 0)))
        bus = mock.Mock(spec=['fmb_send_recv'])
        bus.fmb_send_recv.side_effect = answer
        seen = {9: [], 12: []}
        for step in range(50):
            for addr, *_ in mgr._poll_once(bus)[1]:
                seen[addr].append(step)
        for visits in seen.values():
            self.assertTrue(visits)
            self.assertLessEqual(max(b - a for a, b in zip([-1] + visits, visits + [50])), 12)

    def test_bad_data_after_genuine_zero_flag_preserves_ack(self):
        report = with_crc(bytes.fromhex('054611000106020401d00400'))
        rec = report[6:-2]
        malformed = (
            bytes.fromhex('0a461100'), report[:-2], report[:-1] + bytes([report[-1] ^ 1]),
            with_crc(bytes.fromhex('054611010207') + rec),
            with_crc(bytes.fromhex('05461101010c') + rec * 2),
            with_crc(bytes.fromhex('05461101020c') + bytes.fromhex('020501d00400') + rec),
            with_crc(bytes.fromhex('05461101010b') + rec + rec[:-1]),
            with_crc(bytes.fromhex('f84611010106') + rec),
            with_crc(bytes.fromhex('054511010106') + rec), b'',
        )
        mgr = manager([5])
        bus = SerialReplies([report, *malformed, b''])
        self.assertEqual(mgr._poll_once(bus)[1], [(5, 3, 464, 4)])
        for bad in malformed:
            with self.subTest(bad=bad.hex()):
                self.assertEqual(mgr._poll_once(bus), (False, []))
                self.assertFalse(mgr._last_poll_answered)
                self.assertIsNone(mgr._poll_empty_slave)
                self.assertEqual(bus.sent[-1][0][5:7], bytes((5, 0)))
        mgr._poll_once(bus)
        self.assertEqual(bus.sent[-1][0][5:7], bytes((5, 0)))

    def test_truncated_wb_input_cannot_ack_as_legacy_holding_after_wb_identification(self):
        ambiguous = bytes.fromhex('14 46 11 00 01 05 02 04 00 09 01 ca 7a')
        valid = with_crc(bytes.fromhex('14 46 11 01 01 06 02 04 00 09 01 00'))
        for known in (None, True, False):
            with self.subTest(known=known):
                mgr = manager([20])
                if known is not None:
                    mgr._wb_frame_slaves[20] = known
                bus = SerialReplies([valid, ambiguous, b''])
                self.assertEqual(mgr._poll_once(bus)[1], [(20, 3, 9, 1)])
                if known is False:
                    self.assertEqual(mgr._poll_once(bus)[1], [(20, 2, 1024, 2305)])
                    expected_flag = 0
                else:
                    self.assertEqual(mgr._poll_once(bus), (False, []))
                    self.assertFalse(mgr._last_poll_answered)
                    expected_flag = 1
                mgr._poll_once(bus)
                self.assertEqual(bus.sent[-1][0][5:7], bytes((20, expected_flag)))
        fresh = manager([20])
        self.assertEqual(fresh._poll_once(SerialReplies([ambiguous]))[1], [(20, 2, 1024, 2305)])

    def test_empty_ends_burst_without_acknowledging_unseen_data(self):
        report = with_crc(bytes.fromhex('0c4611550106020400690100'))
        addressed = with_crc(bytes.fromhex('0c4612aa'))
        broadcast = bytes.fromhex('fd4612525d')
        for response, own in ((broadcast, None), (addressed, 12)):
            with self.subTest(empty=response):
                mgr = manager([12])
                bus = SerialReplies([report, b'\xff\xff' + response, b''])
                self.assertTrue(mgr._poll_once(bus)[1])
                self.assertEqual(mgr._poll_once(bus), (False, []))
                self.assertTrue(mgr._last_poll_answered)
                self.assertEqual(mgr._poll_empty_slave, own)
                mgr._poll_once(bus)
                self.assertEqual(bus.sent[-1][0][5:7], bytes((12, 0x55)))

    def test_lost_wide_data_small_empty_and_large_retry(self):
        data = with_crc(bytes.fromhex('0c4611aa020c020400690100020400690200'))
        empty = with_crc(bytes.fromhex('0c4612aa'))
        mgr, bus = manager([12]), SerialReplies([data[:-1], empty, data])
        for credit, response in ((128, False), (6, True), (128, True)):
            mgr.MAX_DATA_LEN = credit
            mgr._poll_once(bus)
            self.assertEqual(bus.sent[-1][0][5:7], b'\x00\x00')
            self.assertEqual(mgr._last_poll_answered, response)
            self.assertIsNone(mgr._poll_empty_slave)
        self.assertEqual((mgr._ack_slave, mgr._ack_flag), (12, 0xaa))

    def test_malformed_responses_leave_last_data_and_own_boundary_untouched(self):
        previous = with_crc(bytes.fromhex('0c4611550106020400690100'))
        next_data = with_crc(bytes.fromhex('0c4611aa0106020400690200'))
        empty = with_crc(bytes.fromhex('0c4612aa'))
        global_end = bytes.fromhex('fd4612525d')
        for response in (next_data+b'x', next_data[:-1], with_crc(bytes.fromhex('0c4611aa0000')),
                         with_crc(bytes.fromhex('0c4611aa010301ff00')), empty+b'x',
                         empty[:-1], global_end+b'x', global_end[:-1],
                         with_crc(bytes.fromhex('0c4612')), with_crc(bytes.fromhex('fd4612aa'))):
            with self.subTest(response=response):
                mgr, bus = manager([12]), SerialReplies([previous, response])
                mgr._poll_once(bus)
                self.assertEqual(mgr._poll_once(bus), (False, []))
                self.assertFalse(mgr._last_poll_answered)
                self.assertIsNone(mgr._poll_empty_slave)
                self.assertEqual((mgr._ack_slave, mgr._ack_flag), (12, 0x55))

    def test_overflow_replaces_data_without_consuming_the_confirmation(self):
        for stuck in ([], [3]):
            with self.subTest(stuck=stuck):
                bus = OverflowLine({a:range(500) for a in (3, 12, 14)}, stuck=stuck)
                mgr = manager(bus.queues)
                for _ in range(150):
                    mgr._poll_once(bus)
                for addr in bus.queues:
                    if addr in stuck:
                        self.assertEqual(bus.confirmed[addr], 0)
                    else:
                        self.assertGreaterEqual(bus.confirmed[addr], 10)
                self.assertGreaterEqual(bus.requests.count(1), 25)

    def test_only_wb_treats_changed_data_as_a_repeat_under_the_same_flag(self):
        # Actual 12AI captures: both carry 0xAA, but the queue head changed.
        a = bytes.fromhex('0c4611aa051e020401cbdb00020401d2db00020401cbda00020401d2da00020401d901018e57')
        b = bytes.fromhex('0c4611aa051e020401d90101020401e00101020401d90201020401e00201020401d90101f0f2')
        zero = [with_crc(packet[:3] + b'\x00' + packet[4:-2]) for packet in (a, b)]
        legacy = [with_crc(bytes.fromhex('0c4611aa010503006900') + bytes([v])) for v in (0, 1)]
        for pair, known_wb, expected in (([a,b], True, 12), ([a,b], False, 13),
                                        (zero, True, 12), (legacy, False, 13), (legacy, True, 13)):
            with self.subTest(known_wb=known_wb, first=pair[0]):
                bus = SerialReplies([*pair, b''])
                mgr = manager([12,13,14])
                mgr._wb_frame_slaves[12] = known_wb
                self.assertTrue(mgr._poll_once(bus)[1])
                self.assertTrue(mgr._poll_once(bus)[1])
                mgr._poll_once(bus)
                self.assertEqual(bus.sent[-1][0][3], expected)

    def test_com4_repeated_batch_gets_one_own_ack_opportunity(self):
        # Captured COM4 12AI DATA and 12AO EMPTY, including arbitration prefixes.
        first = bytes.fromhex('ffffffffffffffff0c461155051e020401c4d700020401c4d800020401c4d700020401c4d800020401c4d7009e3a')
        pending = bytes.fromhex('ffffffffffffffff0c4611aa051e020401c4d800020401cbdb00020401d2db00020401cbda00020401d2da009aad')
        empty = bytes.fromhex('ffffffffff0d4612aa6f82')
        for recovery in (pending, b'', pending[:-1]):
            with self.subTest(recovery=recovery):
                bus = SerialReplies([first, pending, empty, pending, ScanHold(), recovery, b''])
                bus.max_read_regs = 24
                mgr = manager([3, 10, 11, 12, 13, 14])
                for _ in range(4):
                    mgr._poll_once(bus)
                with self.assertRaises(ScanHold):
                    mgr._poll_once(bus)
                mgr._poll_once(bus)
                mgr._poll_once(bus)
                self.assertEqual([tx[0][3] for tx in bus.sent], [1, 1, 13, 1, 12, 12, 14])
                self.assertEqual(bus.sent[4][0], bus.sent[5][0])
                self.assertEqual(bus.sent[5][0][4:7], bytes((33, 12, 0xaa)))

    def test_each_queue_advances_with_winner_only_and_mixed_ack_processing(self):
        for early in ([], [3], [12, 14]):
            with self.subTest(early=early):
                bus = WinnerAckLine({a:range(200) for a in (3, 12, 14)}, early=early)
                mgr = manager(bus.queues)
                advances = {a:[] for a in bus.queues}
                for step in range(150):
                    before = bus.confirmed.copy()
                    mgr._poll_once(bus)
                    for addr in bus.confirmed - before:
                        advances[addr].append(step)
                for records in advances.values():
                    self.assertGreaterEqual(len(records), 10)
                    self.assertLessEqual(max(b-a for a,b in zip([-1]+records, records+[150])), 15)

    def test_stuck_low_queue_cannot_monopolize_recovery(self):
        bus = WinnerAckLine({a:range(200) for a in (3, 12, 14)}, stuck=[3])
        mgr = manager(bus.queues)
        for _ in range(150):
            mgr._poll_once(bus)
        self.assertEqual(bus.confirmed[3], 0)
        self.assertGreaterEqual(min(bus.confirmed[a] for a in (12, 14)), 10)
        self.assertGreaterEqual(bus.requests.count(1), 25)

    def test_three_continuously_busy_nodes_deliver_distinct_values(self):
        bus = Line({3: range(200), 12: range(200), 14: range(200)})
        mgr = manager(bus.queues)
        received = {addr: [] for addr in bus.queues}
        for step in range(100):
            _, events = mgr._poll_once(bus)
            for addr, _, _, value in events:
                received[addr].append((step, value))
        for addr, records in received.items():
            with self.subTest(address=addr):
                self.assertTrue(records)
                self.assertEqual([v for _, v in records], list(range(len(records))))
                self.assertGreaterEqual(bus.confirmed[addr], len(records) - 1)
                steps = [-1] + [step for step, _ in records] + [100]
                self.assertLessEqual(max(b-a for a,b in zip(steps,steps[1:])), 6)
        self.assertIn(1, bus.requests)

    def test_waiter_and_another_high_queue_share_targeted_polls(self):
        bus = Line({3: range(50), 12: [], 14: [123]})
        mgr = manager(bus.queues, waiters=[12])
        empty, visited = set(), []
        for _ in range(12):
            _, events = mgr._poll_once(bus)
            empty.add(mgr._poll_empty_slave)
            visited.extend((e[0], e[3]) for e in events)
        self.assertIn((14, 123), visited)
        self.assertIn(12, empty)
        self.assertIn(14, bus.requests)
        self.assertIn((3, 0), visited)

    def test_new_lower_address_event_survives_the_next_full_poll(self):
        bus = Line({2: [], 9: range(50), 31: range(50)})
        mgr = manager(bus.queues)
        mgr._poll_once(bus)
        mgr._poll_once(bus)
        _, events = mgr._poll_once(bus)
        self.assertEqual([e[0] for e in events], [31])
        bus.queues[2].append(71)
        _, events = mgr._poll_once(bus)
        self.assertEqual([(e[0], e[3]) for e in events], [(2, 71)])

    def test_idle_bus_with_missing_nodes_remains_unrestricted(self):
        bus = Line({3: []})
        mgr = manager([3, 12, 14])
        for response in (None, b'', b'\x03\x46\x11\x55\x01\x06', None):
            bus.fault = response
            mgr._poll_once(bus)
        self.assertEqual(bus.requests, [1] * 4)

    def test_isolated_data_followed_by_empty_does_not_probe_missing_nodes(self):
        bus = Line({3: []})
        mgr = manager([3, 12, 14])
        for value in range(4):
            bus.queues[3].append(value)
            self.assertEqual(mgr._poll_once(bus)[1][0][3], value)
            self.assertEqual(mgr._poll_once(bus)[1], [])
        self.assertEqual(bus.requests, [1] * 8)

    def test_single_waiter_still_gets_its_own_empty(self):
        bus = Line({12: []})
        mgr = manager([12], waiters=[12])
        for _ in range(4):
            mgr._poll_once(bus)
            self.assertEqual(mgr._poll_empty_slave, 12)
        self.assertEqual(bus.requests, [12, 1, 12, 1])

    def test_single_configured_source_and_unarmed_high_address(self):
        bus = Line({3: range(10)})
        mgr = manager([3, 14])
        mgr._devices[14]['configured'] = False
        for _ in range(5):
            mgr._poll_once(bus)
        self.assertEqual(bus.requests, [1] * 5)

    def test_scan_hold_preserves_target_and_ack_then_quiet_resumes_full(self):
        bus = Line({3: [45, 46], 11: [], 14: []})
        mgr = manager(bus.queues)
        mgr._poll_once(bus)
        mgr._poll_once(bus)
        bus.fault = ScanHold
        with self.assertRaises(ScanHold):
            mgr._poll_once(bus)
        for _ in range(4):
            mgr._poll_once(bus)
        self.assertEqual(bus.requests, [1, 1, 11, 1, 1, 1])
        self.assertEqual(bus.confirmed[3], 2)
