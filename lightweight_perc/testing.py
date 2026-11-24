"""For tests, ours and yours: a card that answers from a fixture, one of the real cards' recorded
answers (lightweight_perc/fixtures/), so a test reads a real card's answers and sends no ioctl.
It checks every command as Card does, so a test can never ask for anything the real card
wouldn't be asked for.

    from lightweight_perc import read_controller
    from lightweight_perc.testing import ReplayCard, answers
    perc = read_controller(ReplayCard(answers("perc-h710p-adapter")))

Each fixture also holds what perccli showed in the same minute (load(card)["perccli_metrics"]).
answers() returns bytearrays, so a test can change bytes to make a case the cards didn't show."""

import json
import os
import struct

from . import ioctl

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CARDS = ["perc-h330-adapter", "perc-h330-mini", "perc-h710p-adapter", "perc-h730p-mini"]


def load(card):
    with open(os.path.join(FIXTURES, card + ".json")) as f:
        return json.load(f)


def answers(card):
    """{name: bytearray} from a fixture's answers; PD_GET_INFO as pd_info/<device id>."""
    out = {}
    for name, a in load(card)["answers"].items():
        if "hex" in a:
            out[name] = bytearray.fromhex(a["hex"])
        else:
            for k, v in a.items():
                out[f"{name}/{k}"] = bytearray.fromhex(v["hex"])
    return out


class ReplayCard:
    def __init__(self, answers, status=None):
        self.answers = answers
        self.status = status or {}     # name -> a firmware status to answer with instead
        self.sent = []

    def dcmd(self, opcode, length, mbox=b""):
        name = ioctl.check_read(opcode, length, mbox)
        self.sent.append(name)
        key = {ioctl.CTRL_GET_INFO: "ctrl_info", ioctl.LD_GET_LIST: "ld_list",
               ioctl.PD_LIST_QUERY: "pd_list", ioctl.BBU_GET_STATUS: "bbu_status"}.get(opcode)
        if opcode == ioctl.CFG_READ:
            key = "cfg_read_header" if length == ioctl.CFG_HEADER_SIZE else "cfg_read"
        elif opcode == ioctl.PD_GET_INFO:
            key = f"pd_info/{struct.unpack('<H', mbox)[0]}"
        if self.status.get(key, 0) != 0:
            raise ioctl.PercError(f"{name}: firmware status {self.status[key]:#04x}")
        answer = bytes(self.answers[key])
        if key == "cfg_read" and len(answer) != length:
            raise AssertionError(f"CFG_READ asked for {length}, recorded {len(answer)}")
        return answer[:length] + bytes(max(0, length - len(answer)))
