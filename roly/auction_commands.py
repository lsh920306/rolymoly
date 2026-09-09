"""Validate immutable browser bid intents; all authority remains in LiveAuction."""
from hashlib import sha256
import math
import sqlite3
from uuid import UUID


def context_id(db_path, token, event_id):
    """Partition browser drafts without sending a login token to a component."""
    return sha256(f"{db_path}\0{token or ''}\0{event_id}".encode()).hexdigest()


def clean_envelope(value, expected_context):
    if not isinstance(value, dict) or value.get("context") != expected_context:
        raise ValueError("로그인 또는 경매가 변경되었습니다. 최신 화면에서 다시 입찰해 주세요.")
    epoch, sequence, sent = value.get("epoch"), value.get("seq"), value.get("sent_ms")
    if (not isinstance(epoch, str) or not 1 <= len(epoch) <= 64
            or type(sequence) is not int or not 0 <= sequence <= 2**53 - 1
            or type(sent) not in (int, float) or not math.isfinite(sent) or sent < 0):
        raise ValueError("입찰 연결 정보를 확인하지 못했습니다. 화면을 다시 열어 주세요.")
    envelope = {"context": expected_context, "epoch": epoch, "seq": sequence, "sent_ms": sent}
    command = value.get("command")
    if command is not None:
        if not isinstance(command, dict):
            raise ValueError("입찰 요청 형식이 올바르지 않습니다.")
        request_id, lot_id, amount = command.get("request_id"), command.get("lot_id"), command.get("amount")
        if (not isinstance(request_id, str) or len(request_id) > 64
                or type(lot_id) is not int or lot_id <= 0
                or type(amount) is not int or not 0 <= amount <= 2**53 - 1):
            raise ValueError("입찰 금액과 선수 정보를 확인해 주세요.")
        try:
            UUID(request_id)
        except (ValueError, AttributeError):
            raise ValueError("입찰 요청 번호가 올바르지 않습니다.") from None
        envelope["command"] = {"request_id": request_id, "lot_id": lot_id, "amount": amount}
    return envelope


def execute_command(live, token, event_id, command, *, confirm_only=False):
    """Return a receipt acknowledgment, never an optimistic success.

    Lost acknowledgments replay the same UUID through the domain service.
    Transport/storage uncertainty is explicitly pending and must not unlock
    another browser intent until this one has a definitive outcome.
    """
    ack = {**command, "status": "pending", "message": "입찰 접수를 확인하고 있습니다."}
    try:
        if confirm_only:
            receipt = live.resolve_bid(token, event_id, command["lot_id"], command["amount"], command["request_id"])
            if receipt is None:
                ack.update(status="rejected", message="이전 입찰은 접수되지 않았습니다. 현재 선수와 금액을 확인하고 다시 입찰해 주세요.")
                return ack
        else:
            live.place_bid(token, event_id, command["lot_id"], command["amount"], command["request_id"])
    except (ValueError, PermissionError) as error:
        ack.update(status="rejected", message=str(error))
    except sqlite3.IntegrityError:
        ack.update(status="rejected", message="입찰 정보를 저장하지 못했습니다. 현재 선수와 금액을 확인한 뒤 다시 시도해 주세요.")
    except sqlite3.Error:
        # Never expose database addresses, connection strings or SQL details.
        ack["message"] = "연결을 확인하고 있습니다. 같은 입찰의 접수 여부를 다시 확인합니다."
    else:
        ack.update(status="accepted", message=f"{command['amount']:,} P 입찰을 접수했습니다.")
    return ack
