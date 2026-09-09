"""Late-response and backoff isolation checks against the actual JS channel."""
import unittest

from tests import test_live_panel_component as component_fixtures


class LivePanelDeliveryTests(unittest.TestCase):
    def run_js(self, script, *, dom=False):
        component_fixtures.LivePanelComponentTests.run_js(self, script, dom=dom)

    def test_new_command_does_not_inherit_a_timed_out_read_backoff(self):
        self.run_js(r"""
const first=channel.pump();clock=100;channel.receive(frame(first),1000);
clock=600;const read=channel.pump();
clock=2600;const readRetry=channel.pump();assert.ok(readRetry);
assert.equal(channel.timeouts,1);
clock=2700;const bid=channel.submit(11,10);assert.ok(bid);
clock=5699;assert.equal(channel.pump(),null);
clock=5700;const retry=channel.pump();
assert.ok(retry,'a new command starts its own 3s deadline, not the previous read 6s backoff');
assert.deepEqual(retry.command,bid.command);
assert.equal(channel.commandTiming.attempts,2);
""")

    def test_late_exact_ack_resolves_without_refreshing_an_older_snapshot_clock(self):
        self.run_js(r"""
const first=channel.pump();clock=100;channel.receive(frame(first),1000);
clock=200;const bid=channel.submit(11,10);
clock=300;channel.receive({context:'account-event',frame_id:10,request:null},1000.2);
const lastContact=channel.lastContact, priorClock={...channel.bestClock}, metrics={...channel.metrics};
clock=6200;assert.equal(channel.stale(),true);
const delivered=channel.receive(frame(bid,{...bid.command,status:'accepted'},{frame_id:9}),1000.3);
assert.equal(delivered.fresh,false,'old frame must not replace a newer view');
assert.equal(channel.pending,null,'an exact durable receipt still resolves its original command');
assert.equal(channel.lastContact,lastContact,'old frame cannot make a stale view look connected');
assert.deepEqual(channel.bestClock,priorClock);
assert.deepEqual(channel.metrics,metrics);
assert.equal(channel.stale(),true);
""")

    def test_retry_waits_six_seconds_after_first_timeout_and_keeps_original_uuid(self):
        self.run_js(r"""
const first=channel.pump();clock=100;channel.receive(frame(first),1000);
clock=200;const bid=channel.submit(11,10);
clock=3199;assert.equal(channel.pump(),null);
clock=3200;const second=channel.pump();assert.deepEqual(second.command,bid.command);
for(const tick of [3201,6000,9199]){clock=tick;assert.equal(channel.pump(),null);}
assert.equal(channel.stale(),true);
clock=9200;const third=channel.pump();assert.deepEqual(third.command,bid.command);
assert.equal(channel.commandTiming.attempts,3);
clock=9300;channel.receive(frame(bid,{...bid.command,status:'accepted'}),1008.9);
assert.equal(channel.pending,null);
assert.equal(channel.lastCommandTiming.elapsed_ms,9100);
assert.equal(channel.lastCommandTiming.attempts,3);
assert.equal(channel.commandRoundTripMs,null,'retries must not train the next command timeout');
assert.equal(channel.outstanding.seq,third.seq,'old ACK must not clear the newest in-flight envelope');
""")

    def test_superseded_terminal_ack_cannot_resolve_or_retime_a_new_command(self):
        self.run_js(r"""
const first=channel.pump();clock=100;channel.receive(frame(first),1000);
clock=200;const bid=channel.submit(11,10);
clock=400;channel.receive(frame(bid,{...bid.command,status:'accepted'}),1000.2);
const completed={...channel.lastCommandTiming};
clock=500;const next=channel.submit(11,20);
clock=800;const delivered=channel.receive(frame(bid,{...bid.command,status:'rejected'}),1000.3);
assert.deepEqual(channel.pending,next.command);
assert.equal(channel.commandTiming.started,500);
assert.deepEqual(channel.lastCommandTiming,completed);
assert.equal(channel.outstanding.seq,next.seq);
assert.equal(channel.commandRoundTripMs,200);
""")

    def test_pending_ack_retains_recovery_delay_despite_start_based_read_cadence(self):
        self.run_js(r"""
const first=channel.pump();clock=100;channel.receive(frame(first),1000);
clock=200;const bid=channel.submit(11,10);
clock=1200;channel.receive(frame(bid,{...bid.command,status:'pending'}),1000.9);
assert.equal(channel.nextAt,2400);
for(const tick of [1201,1700,2399]){clock=tick;assert.equal(channel.pump(),null);}
clock=2400;const retry=channel.pump();assert.deepEqual(retry.command,bid.command);
assert.equal(channel.commandTiming.attempts,2);
clock=2600;channel.receive(frame(retry,{...bid.command,status:'rejected'}),1002.4);
assert.equal(channel.pending,null);
assert.equal(channel.commandRoundTripMs,null);
assert.equal(channel.lastCommandTiming.elapsed_ms,2400);
assert.equal(channel.nextAt,2900,'ordinary polling resumes at the last request start +500ms');
""")


if __name__ == "__main__":
    unittest.main()
