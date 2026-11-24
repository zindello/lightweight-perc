# lightweight-perc

Reads a Dell PERC RAID controller (an LSI/Broadcom MegaRAID card) straight from the card, in pure
Python, with no dependencies and no perccli. It only reads: it sends six read-only firmware
commands and nothing else.

It gives you:
- the controller's model and its chip (ROC) temperature;
- each array (virtual disk): its name, RAID level, state, exact size, its cache policy as set and
  as it is now, and its member disks;
- each physical disk: its slot, model, state, exact size and, during a rebuild, how far along it is;
- the battery (BBU), where the card has one: its temperature, voltage, and whether it's too hot or
  charging.

A whole read takes 2 to 9 ms, and no disk stops while it runs.

## How it came about

Two programs on a small cluster of Dell servers read the PERCs with Dell's `perccli`: a monitor that
exports every server's arrays and disks to Prometheus, and the fan controller of a Dell T620.
Both were freezing the disks.

- **The fan controller, on the T620's PERC H710P,** asked perccli for the chip's temperature
  (`/c0 show temperature`) every 30 seconds. Every disk on the card stopped for 0.6 to 3.25 s on
  every read, and etcd logged slow writes.
- **The monitor, on an R430's PERC H730P Mini,** ran perccli's controller summary (`/c0 show`)
  every minute. Every disk on the card stopped for 0.65 to 1.45 s, and the array's latency went
  from 0.1 ms to 120 to 250 ms.

Measured with a probe that read `/proc/diskstats` every 50 ms: a disk counts as stopped when it
has I/O in flight and none completes. perccli's controller summary does far more than read what
it shows, and the card stalls while it does.

The fix was to ask the card only for what we need, the way perccli itself talks to it: one
firmware command (a DCMD) at a time, through the `megaraid_sas` driver's management ioctl. The
first was the chip's temperature, alone. Then every value both programs took from perccli was
read this way, on every card we have, and compared with perccli's in the same minute. They
matched, and no read stopped a disk for even 0.1 s. This library is that code, shared by both
programs.

## What it sends

Only these six commands, each with its fixed answer size and mailbox. Each is a read: the frame
has the read direction (`MFI_FRAME_DIR_READ`), and the card only fills a buffer.

| Command | Opcode | What it answers |
| --- | --- | --- |
| `CTRL_GET_INFO` | `0x01010000` | The controller: model, whether it has a battery, the chip's temperature |
| `LD_GET_LIST` | `0x03010000` | Each array's target id, state and size |
| `CFG_READ` | `0x04010000` | The configuration: each array's name, RAID level, state, cache policy, spans and members |
| `PD_LIST_QUERY` | `0x02010100` | Every device the card sees: its id, enclosure, slot and type |
| `PD_GET_INFO` | `0x02020000` | One disk: inquiry data (vendor, model), state, size, block size, slot, rebuild progress |
| `BBU_GET_STATUS` | `0x05010000` | The battery: temperature, voltage, status (too hot, charging) |

**Nothing else is ever sent.** The allowlist is fixed in `lightweight_perc/ioctl.py` (`READS`).
Any other opcode, size or mailbox raises `ReadOnlyViolation` before the device is opened. There is
no set, start, clear, flash, locate, learn cycle or event wait, and the tests check the exact
bytes of every frame. `BBU_GET_STATUS` is sent only when the controller says it has a battery.

The layouts come from two published sources, and every offset was checked with `offsetof()`
against the headers themselves:
- **Linux** v6.12, `drivers/scsi/megaraid/megaraid_sas.h`, and the driver's own use of each
  command in `megaraid_sas_base.c`;
- **FreeBSD** releng/14.1, `sys/dev/mfi/mfireg.h`, and its RAID tool `mfiutil`, for `CFG_READ` and
  `BBU_GET_STATUS`, which the Linux driver never sends itself.

## The cards it's been checked on

Every value below was read on 24 November 2026 and matched perccli's in the same minute (sizes
apart: perccli rounds them to three decimals; this gives the card's own figure).

| Server | Card | Commands in a whole read | A whole read |
| --- | --- | --- | --- |
| Dell R430 | PERC H330 Mini | 14 | 9 ms |
| Dell R330 | PERC H330 Adapter | 9 | 2 ms |
| Dell R430 | PERC H730P Mini | 15 | 5 ms |
| Dell T620 | PERC H710P Adapter | 16 | 7 ms |

Each card's recorded answers are in `lightweight_perc/fixtures/`, with serial numbers, SAS
addresses and VPD page 83 identifiers zeroed, beside what perccli showed. All 27 disks were SAS. Not seen on these
cards, so tested only by changing recorded bytes: a rebuild in progress, a failed or missing disk
in a working array, a battery charging or too hot, and SATA disks.

## Using it

### Install

It's on GitHub, not PyPI. Pin it to a release tag; the tags never move.

```text
# requirements file
lightweight-perc @ git+https://github.com/zindello/lightweight-perc@v0.1.0
```

```sh
pip install --no-deps "lightweight-perc @ git+https://github.com/zindello/lightweight-perc@v0.1.0"
```

pip needs `git` to fetch it. Building it needs nothing else: the build backend is in the repo
(`_build/backend.py`), so pip fetches nothing from PyPI. Python 3.11 or newer.

### Where it runs

On Linux, as root (or with `CAP_SYS_ADMIN` and access to the device), with the `megaraid_sas`
driver loaded and exactly one MegaRAID card. In a container, the container needs the device:
`/dev/megaraid_sas_ioctl_node`. Some systems (Talos Linux, for one) register the driver's ioctl
device but never make the node; `ensure_megaraid_node()` makes it, as perccli does, from the
driver's major number in `/proc/devices`. That needs a container allowed to make device nodes.

### Read the card

```python
from lightweight_perc import PercError, ensure_megaraid_node, read_controller

ensure_megaraid_node()
try:
    perc = read_controller()
except PercError as e:
    print("couldn't read the card:", e)
else:
    print(perc.model, perc.chip_celsius, "°C")
    for target, array in perc.arrays.items():
        print(target, array.name, array.raid_type, array.state_name, array.size_bytes)
        for m in array.members:
            print("  member", m.device_id, m.enclosure, m.slot)     # device_id None: no disk there
    for disk in perc.disks.values():
        print(disk.enclosure, disk.slot, disk.model, disk.state_name, disk.size_bytes,
              disk.array, disk.rebuild_percent)
    if perc.battery:
        print("battery", perc.battery.temperature_celsius, "°C",
              "too hot" if perc.battery.temp_high else "", "charging" if perc.battery.charging else "")
```

Ask only for what you need, and fewer commands are sent:

```python
read_controller(battery=False)                # arrays with sizes, and disks: no BBU_GET_STATUS
read_controller(sizes=False, disks=False)     # the chip, the arrays' state and cache, the battery:
                                              # CTRL_GET_INFO, CFG_READ twice, BBU_GET_STATUS
```

`read_controller()` returns everything it was asked for, or raises `PercError`. It never returns
part of a read. A read fails when:
- there isn't exactly one `megaraid_sas` host, or the device can't be opened;
- the ioctl fails, or the firmware's status isn't OK for any command;
- the controller's answer isn't a PERC's (the vendor isn't LSI, `0x1000`, or there's no model);
- a count runs past its answer, the configuration's size is under its header or over 64 KiB, the
  array list and the configuration name different arrays, or an array member isn't among the
  disks (a disk pulled mid-read: the next read is clean).

A chip temperature of 0 or 255 is no reading: `chip_celsius` is `None`. The ioctl has no timeout
of its own: the driver waits for the firmware. If that matters to you, read in a thread and
treat a read that doesn't return as a failure.

### What the values mean

- **`Array.state`:** 3 optimal, 2 degraded, 1 partially degraded, 0 offline. Any other number is
  kept, and `state_name` gives it as `state <n>`.
- **`Array.raid_type`:** RAID0, 1, 5 or 6, as perccli writes it, with a 0 added when the array
  spans more than one disk group (RAID10, 50, 60).
- **`Array.members`:** in span order. A member slot with no disk in it has `device_id` `None`;
  that's how the card shows a missing disk, and also the empty slots of an array taken apart.
- **`Disk.fw_state`:** 0x18 online, 0x14 rebuilding, 0x40 jbod, 0x00 unconfigured, 0x02 hot
  spare, 0x10 offline, 0x11 failed, 0x01 bad, 0x20 copying back. `state_name` gives the word, or
  `state 0x..` for anything else.
- **`Disk.size_bytes`:** the coerced size times the disk's block size (512 when the card leaves it
  0, as the H710P does). 4K disks report their size in 4096-byte blocks.
- **`Disk.model`:** the product field of the disk's inquiry data, which is what perccli shows for
  SAS disks. `Disk.vendor` is the vendor field.
- **`Disk.rebuild_percent`:** while a rebuild is active, its progress rounded down to a whole
  percent, as perccli shows it; otherwise `None`.
- **`Disk.array`:** the target id of the array it's a member of, or `None`.
- **`Battery.temp_high`, `Battery.charging`:** the battery's firmware status bits 2 and 3.

### Testing your own code against real cards

`lightweight_perc.testing` replays a real card's recorded answers, so your tests read what a card
really said and send no ioctl:

```python
from lightweight_perc import read_controller
from lightweight_perc.testing import CARDS, ReplayCard, answers, load

perc = read_controller(ReplayCard(answers("perc-h710p-adapter")))
perccli_said = load("perc-h710p-adapter")["perccli_metrics"]["lines"]
```

`answers()` gives each answer as a `bytearray`, so a test can change bytes to make a case the cards
didn't show (a failed disk, a rebuild). `ReplayCard(answers, status={"pd_list": 0x03})` answers
with a firmware error instead.

## Developing

```sh
python3 -m unittest discover -s tests
```

The tests need no card and send no ioctl: they replay each card's recorded answers
(`lightweight_perc.testing`), and build the other cases by changing their bytes. CI runs them on GitHub's runners on every pull request and
on `main`.

**Releasing:** bump `version` in `pyproject.toml` and `__version__` in
`lightweight_perc/__init__.py` (a test checks they match) in a pull request. Once it's merged,
tag the merge on `main`: `git tag -a v<version> -m v<version> && git push origin v<version>`.
Tags can't be moved or deleted.
