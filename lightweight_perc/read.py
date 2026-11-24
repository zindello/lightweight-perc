"""One whole read of a card: everything it was asked for, or PercError. Never part of a read."""

import dataclasses
import struct
from dataclasses import dataclass

from .ioctl import (BBU_GET_STATUS, BBU_STATUS_SIZE, CFG_HEADER_SIZE, CFG_READ, CTRL_GET_INFO,
                    CTRL_INFO_SIZE, LD_GET_LIST, LD_LIST_SIZE, NODE, PD_GET_INFO, PD_INFO_SIZE,
                    PD_LIST_QUERY, PD_LIST_SIZE, SYS_SCSI_HOST, Card, PercError)
from .parse import (Array, BatteryStatus, Disk, cfg_size, parse_bbu_status, parse_config,
                    parse_ctrl_info, parse_ld_list, parse_pd_info, parse_pd_list)


@dataclass(frozen=True)
class Controller:
    model: str
    chip_celsius: int | None             # None when the card gives 0 or 255
    battery_present: bool
    arrays: dict[int, Array]             # by target id (perccli's VD number)
    disks: dict[int, Disk] | None        # by device id; None when not asked for
    battery: BatteryStatus | None        # None when not asked for, or there's no battery


def read_controller(card=None, *, sizes=True, disks=True, battery=True,
                    node=NODE, sys_scsi_host=SYS_SCSI_HOST):
    """Reads the card. Always: CTRL_GET_INFO and CFG_READ (its header, then the whole of it).
    sizes: LD_GET_LIST, for each array's size. disks: PD_LIST_QUERY, then PD_GET_INFO for each
    disk. battery: BBU_GET_STATUS, only when the controller says it has one.

    card: anything with dcmd(opcode, length, mbox) -> bytes, such as Card; by default a Card on
    node, opened for this read and closed after it."""
    if card is None:
        with Card(node, sys_scsi_host) as c:
            return read_controller(c, sizes=sizes, disks=disks, battery=battery)

    info = parse_ctrl_info(card.dcmd(CTRL_GET_INFO, CTRL_INFO_SIZE))
    sizes_by_target = parse_ld_list(card.dcmd(LD_GET_LIST, LD_LIST_SIZE)) if sizes else None
    size = cfg_size(card.dcmd(CFG_READ, CFG_HEADER_SIZE))
    arrays = parse_config(card.dcmd(CFG_READ, size))

    if sizes_by_target is not None:
        if set(sizes_by_target) != set(arrays):
            raise PercError(f"the array list {sorted(sizes_by_target)} and the configuration "
                            f"{sorted(arrays)} differ")
        arrays = {t: dataclasses.replace(a, size_bytes=sizes_by_target[t][1]) for t, a in arrays.items()}

    found = None
    if disks:
        member_of = {m.device_id: t for t, a in arrays.items() for m in a.members
                     if m.device_id is not None}
        found = {}
        for address in parse_pd_list(card.dcmd(PD_LIST_QUERY, PD_LIST_SIZE, b"\x00")):
            if address.scsi_type != 0:
                continue                                    # enclosures and the like
            disk = parse_pd_info(card.dcmd(PD_GET_INFO, PD_INFO_SIZE,
                                           struct.pack("<H", address.device_id)))
            if disk.device_id != address.device_id:
                raise PercError(f"PD_GET_INFO for {address.device_id} answered for {disk.device_id}")
            found[disk.device_id] = dataclasses.replace(disk, array=member_of.get(disk.device_id))
        gone = set(member_of) - set(found)
        if gone:
            raise PercError(f"array members {sorted(gone)} aren't in the device list")

    bbu = None
    if battery and info.battery_present:
        bbu = parse_bbu_status(card.dcmd(BBU_GET_STATUS, BBU_STATUS_SIZE))

    return Controller(model=info.model, chip_celsius=info.chip_celsius,
                      battery_present=info.battery_present, arrays=arrays, disks=found, battery=bbu)
