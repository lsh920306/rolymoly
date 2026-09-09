"""AppTest protocol driver; browser/Node tests separately execute the real JS.

    Local edits below deliberately cause no Python rerun. Only a command/poll
    enters Streamlit's actual CCv2 trigger aggregator and registered callback.
"""
from copy import deepcopy
import json
from uuid import uuid4

from streamlit.components.v2.bidi_component.main import _make_trigger_id


class PanelClient:
    def __init__(self, app):
        self.app = app
        self.identity = None
        self.draft = 0
        self.pending = None
        self.epoch = str(uuid4())
        self.sequence = 0
        self.request_id = str(uuid4())

    @property
    def element(self):
        return next(item for item in self.app.get("bidi_component")
                    if json.loads(item.proto.json).get("transport") is not None)

    def sync(self):
        data = json.loads(self.element.proto.json)
        identity = (data["transport"]["context"], (data.get("lot") or {}).get("id"))
        if self.identity != identity:
            if not self.identity or self.identity[0] != identity[0]:
                self.pending = None
            self.identity = identity
            self.draft = int((data.get("lot") or {}).get("highest_bid") or 0)
            self.request_id = str(uuid4())
        ack = data["transport"].get("ack")
        if ack and self.pending and ack["request_id"] == self.pending["request_id"] and ack["status"] != "pending":
            self.pending = None
            self.request_id = str(uuid4())
        return data

    def send(self, command=None, *, envelope=None):
        data = self.sync()
        self.sequence += 1
        envelope = envelope or {"context": data["transport"]["context"], "epoch": self.epoch,
                                "seq": self.sequence, "sent_ms": 100.0, "command": command}
        states = deepcopy(self.app._tree.get_widget_states())
        # AppTest has no CCv2 frontend to retain the component's persistent
        # base widget. Its presenter needs that mapping to expose triggers.
        base = next((item for item in states.widgets if item.id == self.element.proto.id), None)
        if base is None:
            base = states.widgets.add(id=self.element.proto.id)
            base.json_value = "{}"
        key = _make_trigger_id(self.element.proto.id, "events")
        widget = next((item for item in states.widgets if item.id == key), None)
        if widget is None:
            widget = states.widgets.add(id=key)
        widget.json_trigger_value = json.dumps([{"event": "event", "value": envelope}])
        self.app._run(states)
        # A reset or logout can intentionally unmount the entire live panel.
        # Keep the protocol driver's pending intent until another panel mounts.
        if any(json.loads(item.proto.json).get("transport") is not None
               for item in self.app.get("bidi_component")):
            self.sync()
        return self.app

    def poll(self):
        return self.send(self.pending)

    def submit(self):
        data = self.sync()
        self.pending = self.pending or {"request_id": self.request_id,
            "lot_id": data["lot"]["id"], "amount": self.draft}
        return self.send(self.pending)

    def widget(self, kind, label):
        self.sync()
        return LocalControl(self, kind, label)


class LocalControl:
    def __init__(self, client, kind, label):
        self.client, self.kind, self.name = client, kind, label

    @property
    def value(self):
        self.client.sync()
        return self.client.draft

    @property
    def label(self):
        return f"{self.value:,} P 입찰하기" if self.name == "입찰하기" else self.name

    @property
    def disabled(self):
        data = self.client.sync()
        return not (data.get("control") or {}).get("can_bid", False) or bool(self.client.pending)

    def set_value(self, value):
        self.client.sync()
        self.client.draft = int(value)
        return self

    def click(self):
        data = self.client.sync()
        highest = int((data.get("lot") or {}).get("highest_bid") or 0)
        if self.name == "입찰하기":
            self.client.submit()
        elif self.name == "금액 초기화":
            self.client.draft = highest
        else:
            self.client.draft = max(self.client.draft, highest) + int(self.name[1:])
        return self

    def run(self):
        return self.client.app


def panel_client(app):
    if not hasattr(app, "_live_panel_test_client"):
        app._live_panel_test_client = PanelClient(app)
    client = app._live_panel_test_client
    client.sync()
    return client
