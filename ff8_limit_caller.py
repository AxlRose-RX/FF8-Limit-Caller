#!/usr/bin/env python3
"""
ff8_limit_caller.py

Limit Break caller for FF8 (OG PC build: 2000 / 2013, FF8_EN.exe, with FFNx).
Companion to FF8 Summon Caller and FF8 Battle Teleport.

How it works
------------
Same trick as the Summon Caller. FF8 fills a small "pending action" buffer when
you pick a command in the battle menu (0x1D28D44, one 0x18 block per party
slot). This tool writes the limit into that buffer, the same way the battle
menu does, and the game runs it on that character's turn:

    <H target><B slot><B command><B arg><B extra1><B extra2><B active=1>

Limit commands (kernel battle command ids, checked in the exe):
    0x05 Renzokuken          0x0B Duel            0x0E Shot (arg = ammo item 101..108)
    0x0F Blue Magic (arg 0..15)                   0x10 Slot (arg = magic id 1..56)
    0x13 Combine (extra1 = move 0..3; arg 1 + extra1 4 = Angel Wing)
    0x11 Fire Cross, 0x12 Sorcery, 0x14/0x15/0x16 Laguna/Kiros/Ward (arg = guest limit 0..4)
    0xF4 non-junctionable GF attack (Angelo Rush/Recover/Reverse/Search = 11..14)

What the game decides while a limit plays, and how the tool steers it:
    - Crisis level [0x1D27BDA + slot*0xD0] is set to 4 before every limit, like a
      full crisis (Renzokuken can only roll a finisher in a crisis).
    - Squall: the finisher is rolled with the battle RNG (counter 0x1D2A228 +
      stream [0x1D2A230], table 0xB697F8). The tool sets the counter so the roll
      lands on the finisher you picked and, for that one attack, lets his gunblade
      use all four finishers (kernel weapon byte). The byte is put back right after.
    - Zell: during a Duel the game waits for the next combo on [0x1D28DEF] (0xFE).
      The tool writes the move there, the same as you entering the buttons.
    - Irvine: with no ammo of that type, the shot count [0x1D2A220] is set to 10.
    - The "used" flags the game saves for Irvine's shots [0x1CFE770] and Selphie's
      special Slot spells [0x1CFE771] are put back afterwards.

Use it
------
1. Start FF8 (OG PC build) with FFNx, get into a battle with the character.
2. Attach to FF8.
3. When that character's command menu is up, click a limit. The tool finds
   which slot the character is in on its own.

Notes
-----
- OG PC build only (image base 0x400000). It refuses to write on any other
  build, so it can't crash the wrong exe.
- Live memory access needs pymem and is Windows-only.

    pip install pymem
    python ff8_limit_caller.py
"""

import sys
import time
import struct
from pathlib import Path
import tkinter as tk
from tkinter import ttk

APP_VERSION = "2026.1001"   # release version (YYYY.MMDD); the GitHub build reads it from here


def get_exe_dir():
    """Folder of the running .exe (when frozen by PyInstaller) or of this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------- addresses
# FF8 OG PC is a non-relocatable exe (loads at 0x400000), so these are absolute
# virtual addresses, not module-relative offsets.
PENDING_BUFFER      = 0x1D28D44   # battle pending-action buffer
SLOT_STRIDE         = 0x18        # each party slot owns a 3-entry (0x18) block
IMAGE_BASE_EXPECTED = 0x400000
PROC_NAMES          = ["FF8.exe", "FF8_EN.exe"]

# battle combatants: 0xD0 each, party = 0..2, enemies = 3..6
CB_STRIDE  = 0xD0
CB_FLAGS   = 0x1D27B8C   # dword, bit 0 = in the battle
CB_STATE   = 0x1D27B90   # byte, & 5 = KO / gone
CB_CHAR_ID = 0x1D27BCB   # byte, character id of a party slot
CB_CRISIS  = 0x1D27BDA   # byte, crisis level 0..4

# battle copy of each party member: 0x1D0 each
BCHAR_STRIDE = 0x1D0
BCHAR_WEAPON = 0x1CFF1BA  # byte, equipped weapon id

# kernel.bin is loaded whole at 0x1CF3E48; its data sections sit at fixed addresses
K_MAGIC      = 0x1CF4064   # 60 bytes each, +0x0A target info (index = magic id)
K_WEAPON     = 0x1CF7400   # 12 bytes each, +0x02 Renzokuken finisher bits
K_CHAR       = 0x1CF75EC   # 36 bytes each, +0x04 limit command
K_TEMP_LIMIT = 0x1CF82C8   # 24 bytes each, +0x0A target info (guest limits)
K_BLUE_MAGIC = 0x1CF8340   # 16 bytes each, +0x09 target info
K_SHOT       = 0x1CF8640   # 24 bytes each, +0x0A target info
K_DUEL_NODES = 0x1CF8840   #  4 bytes each: move, next, next, next
K_COMBINE    = 0x1CF88B4   # 20 bytes each, +0x08 target info
WEAPON_COUNT    = 33
DUEL_NODE_COUNT = 25

# battle RNG: value = table[counter[stream]], then counter + 1
RNG_TABLE   = 0xB697F8     # 256 bytes
RNG_COUNTER = 0x1D2A228    # 8 counters, one byte each
RNG_STREAM  = 0x1D2A230    # which counter this battle uses (0..7)

# limit state the game keeps while a limit plays
G_FINISHER  = 0x1D28E2E    # finisher Renzokuken rolled (0..3)
G_DUEL_NEXT = 0x1D28DEF    # 0xFE = Duel waiting for a combo, 0xFF = time up, else node
G_DUEL_DONE = 0x1D28E1C    # moves done in this Duel (0 = no Duel running)
G_SHOT_ON   = 0x1D28E25    # 1 = Shot started, more while firing, 0 = done
G_SHOT_TYPE = 0x1D28E24    # shot type 0..7
G_SHOT_LEFT = 0x1D2A220    # shots left (from the ammo count)

# save data the game updates when these limits are used
SAVE_IRVINE_SHOTS = 0x1CFE770
SAVE_SELPHIE_SLOT = 0x1CFE771

# pending entry layout (little-endian):
#   <H target_mask><B attacker_slot><B command><B arg><B extra1><B extra2><B active>
CMD_RENZOKUKEN = 0x05
CMD_DUEL       = 0x0B
CMD_SHOT       = 0x0E
CMD_BLUE_MAGIC = 0x0F
CMD_SLOT       = 0x10
CMD_COMBINE    = 0x13
CMD_NJGF       = 0xF4      # non-junctionable GF attack (Angelo on his own)

CRISIS_MAX           = 4
FINISHER_ODDS        = 60 * CRISIS_MAX   # a finisher comes out when the dice <= crisis * 60
NO_AMMO_SHOTS        = 10
DUEL_START_SECONDS   = 15
TICK_MS              = 25
ONE_ENEMY            = 0x50              # kernel target info: one enemy
FALLBACK_TARGET_INFO = ONE_ENEMY

SQUALL, ZELL, IRVINE, QUISTIS, RINOA, SELPHIE, SEIFER, EDEA, LAGUNA, KIROS, WARD = range(11)
CHAR_NAMES = ["Squall", "Zell", "Irvine", "Quistis", "Rinoa", "Selphie",
              "Seifer", "Edea", "Laguna", "Kiros", "Ward"]

FINISHERS = ["Rough Divide", "Fated Circle", "Blasting Zone", "Lion Heart"]

DUEL_MOVES = ["Punch Rush", "Booya", "Heel Drop", "Mach Kick", "Dolphin Blow",
              "Meteor Strike", "Burning Rave", "Meteor Barret", "Different Beat",
              "My Final Heaven"]
DUEL_FIRST_FINISHER = 6

# shot type -> ammo item (101 + type)
SHOTS = [("Normal Shot", "Normal Ammo"), ("Scatter Shot", "Shotgun Ammo"),
         ("Dark Shot", "Dark Ammo"), ("Flame Shot", "Fire Ammo"),
         ("Canister Shot", "Demolition Ammo"), ("Quick Shot", "Fast Ammo"),
         ("Armor Shot", "AP Ammo"), ("Hyper Shot", "Pulse Ammo")]
AMMO_FIRST_ITEM = 101

BLUE_MAGIC = ["Laser Eye", "Ultra Waves", "Electrocute", "LV?Death", "Degenerator",
              "Aqua Breath", "Micro Missiles", "Acid", "Gatling Gun", "Fire Breath",
              "Bad Breath", "White Wind", "Homing Laser", "Mighty Guard", "Ray-Bomb",
              "Shockwave Pulsar"]

COMBINE = ["Angelo Cannon", "Angelo Strike", "Invincible Moon", "Wishing Star"]
ANGEL_WING = 4
# (name, non-junctionable GF attack index, targets an ally)
ANGELO_SOLO = [("Angelo Rush", 11, False), ("Angelo Recover", 12, True),
               ("Angelo Reverse", 13, True), ("Angelo Search", 14, False)]

# (character, limit command, guest limit index, name)
GUESTS = [(SEIFER, 0x11, 0, "No Mercy"), (EDEA, 0x12, 1, "Ice Strike"),
          (LAGUNA, 0x14, 2, "Desperado"), (KIROS, 0x15, 3, "Blood Pain"),
          (WARD, 0x16, 4, "Massive Anchor")]
GUEST_COMMANDS = {0x11, 0x12, 0x14, 0x15, 0x16}

# magic id = index + 1
MAGIC_NAMES = ["Fire", "Fira", "Firaga", "Blizzard", "Blizzara", "Blizzaga", "Thunder",
               "Thundara", "Thundaga", "Water", "Aero", "Bio", "Demi", "Holy", "Flare",
               "Meteor", "Quake", "Tornado", "Ultima", "Apocalypse", "Cure", "Cura",
               "Curaga", "Life", "Full-Life", "Regen", "Esuna", "Dispel", "Protect",
               "Shell", "Reflect", "Aura", "Double", "Triple", "Haste", "Slow", "Stop",
               "Blind", "Confuse", "Sleep", "Silence", "Break", "Death", "Drain", "Pain",
               "Berserk", "Float", "Zombie", "Meltdown", "Scan", "Full-Cure", "Wall",
               "Rapture", "Percent", "Catastrophe", "The End"]
SLOT_SPECIALS = [(51, "Full-Cure"), (52, "Wall"), (53, "Rapture"), (56, "The End")]


# ---------------------------------------------------------------- memory
class Mem:
    def __init__(self, log):
        self.pm = None
        self.log = log
        self.base = None
        self.name = None
        self.saved = {}          # addr -> original bytes that must be put back

    def attached(self):
        return self.pm is not None

    def attach(self):
        try:
            import pymem
        except ImportError:
            self.log("pymem is not installed. Run:  pip install pymem")
            return False

        last = None
        for name in PROC_NAMES:
            try:
                self.pm = pymem.Pymem(name)
                self.name = name
                break
            except Exception as e:
                last = e
                self.pm = None

        if not self.pm:
            self.log(f"FF8 not found ({', '.join(PROC_NAMES)}). Is it running? ({last})")
            return False

        try:
            self.base = self.pm.base_address
        except Exception:
            self.base = None

        self.log(f"Attached: {self.name} (PID {self.pm.process_id}).")
        if self.base not in (None, IMAGE_BASE_EXPECTED):
            self.log(f"WARNING: image base is 0x{self.base:X}, expected 0x{IMAGE_BASE_EXPECTED:X}. "
                     "This is not the OG PC build; writes are disabled to avoid a crash.")
        return True

    def detach(self):
        self.restore_all()
        self.pm = None
        self.base = None
        self.name = None

    def can_write(self):
        return self.pm is not None and self.base in (None, IMAGE_BASE_EXPECTED)

    # ---- reads
    def read(self, addr, size):
        if not self.pm:
            return None
        try:
            data = self.pm.read_bytes(addr, size)
        except Exception:
            return None
        return data if data is not None and len(data) == size else None

    def u8(self, addr):
        data = self.read(addr, 1)
        return data[0] if data else None

    def u32(self, addr):
        data = self.read(addr, 4)
        return struct.unpack("<I", data)[0] if data else None

    # ---- writes
    def write(self, addr, data):
        if not self.pm:
            self.log("Attach to FF8 first.")
            return False
        if not self.can_write():
            self.log("Writes disabled: this is not the OG PC build (image base 0x400000).")
            return False
        try:
            self.pm.write_bytes(addr, data, len(data))
            return True
        except Exception as e:
            self.log(f"Write failed: {e}")
            return False

    def write_u8(self, addr, value):
        return self.write(addr, bytes([value & 0xFF]))

    def write_pending(self, slot, command, arg, target, extra1=0, extra2=0):
        entry = struct.pack("<HBBBBBB", target & 0xFFFF, slot & 0xFF, command & 0xFF,
                            arg & 0xFF, extra1 & 0xFF, extra2 & 0xFF, 1)
        return self.write(PENDING_BUFFER + slot * SLOT_STRIDE, entry)

    # ---- temporary changes that get put back
    def remember(self, addr, size=1):
        if addr not in self.saved:
            data = self.read(addr, size)
            if data is None:
                return False
            self.saved[addr] = data
        return True

    def patch(self, addr, data):
        return self.remember(addr, len(data)) and self.write(addr, data)

    def restore(self, addr):
        data = self.saved.pop(addr, None)
        if data is not None and self.can_write():
            try:
                self.pm.write_bytes(addr, data, len(data))
            except Exception:
                pass

    def restore_all(self):
        for addr in list(self.saved):
            self.restore(addr)


# ---------------------------------------------------------------- battle helpers
def party(mem):
    """Character id in each party slot (None if the slot is empty)."""
    out = []
    for slot in range(3):
        flags = mem.u32(CB_FLAGS + slot * CB_STRIDE)
        char = mem.u8(CB_CHAR_ID + slot * CB_STRIDE)
        ok = flags is not None and char is not None and flags & 1 and char < len(CHAR_NAMES)
        out.append(char if ok else None)
    return out


def alive(mem, index):
    flags = mem.u32(CB_FLAGS + index * CB_STRIDE)
    state = mem.u8(CB_STATE + index * CB_STRIDE)
    return bool(flags is not None and state is not None and flags & 1 and not state & 5)


def default_target(mem, info, caster):
    """The target the battle menu builds from a kernel 'target info' byte."""
    target = 0
    if info & 0x01:
        target |= 0x4000
    if info & 0x02:
        target |= 0x2000
    side = info & 0x30
    if side == 0x00:                       # a whole side
        target |= 0x80F8 if info & 0x40 else 0x8007
    elif side == 0x10:                     # one target
        if info & 0x40:
            index = next((i for i in range(3, 7) if alive(mem, i)), 3)
        elif alive(mem, caster):
            index = caster
        else:
            index = next((i for i in range(3) if alive(mem, i)), caster)
        target |= 1 << index
    elif side == 0x20:                     # everyone
        target |= 0x80FF
    else:
        target |= side
    return target


def best_rng_counter(table, finisher, odds=FINISHER_ODDS):
    """Counter value that makes Renzokuken roll `finisher`.

    Renzokuken rolls three dice in a row: hit pattern, finisher or not
    (dice <= odds), and which finisher (dice % 4 when all four are allowed).
    A counter that still works if the game rolls one or two other dice first
    is preferred."""
    if not table or len(table) != 256:
        return None
    best, best_run = None, 0
    for counter in range(256):
        run = 0
        while (run < 4 and table[(counter + run + 1) & 0xFF] <= odds
               and table[(counter + run + 2) & 0xFF] % 4 == finisher):
            run += 1
        if run > best_run:
            best, best_run = counter, run
    return best


def duel_node(mem, move):
    """A Duel node for `move`: what the game gets when you enter that move's combo."""
    data = mem.read(K_DUEL_NODES, DUEL_NODE_COUNT * 4)
    if not data:
        return None
    for node in range(DUEL_NODE_COUNT):
        if data[node * 4] == move:
            return node
    return None


# ---------------------------------------------------------------- limit caller
class LimitCaller:
    """Everything that talks to the game. The window only calls these."""

    def __init__(self, mem, log):
        self.mem = mem
        self.log = log
        self.jobs = []
        self.duel_queued_at = 0.0

    # ---- watchers (the window polls tick() every TICK_MS)
    def watch(self, kind, step, seconds, cleanup=None, on_timeout=None):
        self.jobs.append({"kind": kind, "step": step, "until": time.monotonic() + seconds,
                          "cleanup": cleanup, "on_timeout": on_timeout})

    def drop(self, kind=None):
        for job in [j for j in self.jobs if kind is None or j["kind"] == kind]:
            self.jobs.remove(job)
            if job["cleanup"]:
                job["cleanup"]()

    def tick(self):
        now = time.monotonic()
        for job in list(self.jobs):
            try:
                done = job["step"]()
            except Exception as e:
                self.log(f"Watcher stopped: {e}")
                if job["cleanup"]:
                    job["cleanup"]()
                done = True
            if done:
                self.jobs.remove(job)
            elif now > job["until"]:
                self.jobs.remove(job)
                if job["cleanup"]:
                    job["cleanup"]()
                if job["on_timeout"]:
                    job["on_timeout"]()

    # ---- helpers
    def slot_of(self, char):
        if not self.mem.attached():
            self.log("Attach to FF8 first.")
            return None
        if not self.mem.can_write():
            self.log("Writes disabled: this is not the OG PC build (image base 0x400000).")
            return None
        members = party(self.mem)
        if char in members:
            return members.index(char)
        self.log(f"{CHAR_NAMES[char]} isn't in the battle party.")
        return None

    def info(self, table, stride, index, offset):
        value = self.mem.u8(table + index * stride + offset)
        return FALLBACK_TARGET_INFO if value is None else value

    def queue(self, char, command, arg, info, label, extra1=0):
        slot = self.slot_of(char)
        if slot is None:
            return None
        self.mem.write_u8(CB_CRISIS + slot * CB_STRIDE, CRISIS_MAX)
        if not self.mem.write_pending(slot, command, arg,
                                      default_target(self.mem, info, slot), extra1):
            return None
        self.log(f"Queued {label} for {CHAR_NAMES[char]} (slot {slot}).")
        return slot

    # ---- Squall
    def renzokuken(self):
        self.queue(SQUALL, CMD_RENZOKUKEN, 0, ONE_ENEMY,
                   "Renzokuken")

    def finisher(self, which):
        slot = self.slot_of(SQUALL)
        if slot is None:
            return
        mem = self.mem
        self.drop("squall")
        table = mem.read(RNG_TABLE, 256)
        stream = mem.u8(RNG_STREAM)
        counter = best_rng_counter(table, which)
        if counter is None or stream is None:
            self.log("Couldn't read the battle dice. Try again in a battle.")
            return
        weapon = mem.u8(BCHAR_WEAPON + slot * BCHAR_STRIDE)
        bits = K_WEAPON + weapon * 12 + 2 if weapon is not None and weapon < WEAPON_COUNT else None

        def put_back():
            if bits is not None:
                mem.restore(bits)

        if bits is not None:
            mem.patch(bits, b"\x0f")                          # all four finishers, this attack only
        mem.write_u8(G_FINISHER, 0xFF)                        # lets us see when the roll happens
        mem.write_u8(CB_CRISIS + slot * CB_STRIDE, CRISIS_MAX)
        mem.write_u8(RNG_COUNTER + (stream & 7), counter)
        info = ONE_ENEMY
        if not mem.write_pending(slot, CMD_RENZOKUKEN, 0, default_target(mem, info, slot)):
            put_back()
            return
        name = FINISHERS[which]
        self.log(f"Queued Renzokuken ending in {name} for Squall (slot {slot}).")

        def step():
            rolled = mem.u8(G_FINISHER)
            if rolled == 0xFF:
                return False
            put_back()
            if rolled == which:
                self.log(f"Renzokuken rolled {name}.")
            elif rolled is not None and rolled < len(FINISHERS):
                self.log(f"Renzokuken rolled {FINISHERS[rolled]} instead (something else used the "
                         "dice first). Try again on Squall's next turn.")
            return True

        self.watch("squall", step, 20, put_back,
                   lambda: self.log("No finisher came out, or it wasn't Squall's turn. "
                                    "Try again when his menu is up."))

    # ---- Zell
    def duel(self):
        if self.queue(ZELL, CMD_DUEL, 0, ONE_ENEMY, "Duel") is not None:
            self.duel_queued_at = time.monotonic()

    def duel_move(self, move):
        slot = self.slot_of(ZELL)
        if slot is None:
            return
        mem = self.mem
        name = DUEL_MOVES[move]
        node = duel_node(mem, move)
        if node is None:
            self.log(f"Couldn't find {name} in the Duel table.")
            return
        running = (mem.u8(G_DUEL_DONE) or 0) >= 1
        starting = time.monotonic() - self.duel_queued_at < DUEL_START_SECONDS
        if not running and not starting:
            mem.write_u8(CB_CRISIS + slot * CB_STRIDE, CRISIS_MAX)
            info = ONE_ENEMY
            if not mem.write_pending(slot, CMD_DUEL, 0, default_target(mem, info, slot)):
                return
            self.duel_queued_at = time.monotonic()
            self.log(f"Queued a Duel for Zell (slot {slot}). {name} comes after his opening move.")

        def step():
            moves = mem.u8(G_DUEL_DONE)
            waiting = mem.u8(G_DUEL_NEXT)
            if moves is None or waiting is None:
                return True
            if moves >= 1 and waiting == 0xFE:
                if mem.write_u8(G_DUEL_NEXT, node):
                    self.log(f"Zell: {name}!")
                return True
            return False

        self.watch("zell", step, DUEL_START_SECONDS,
                   on_timeout=lambda: self.log(f"The Duel never waited for a move, so {name} wasn't "
                                               "sent. Click it again during Zell's Duel."))

    # ---- Irvine
    def shot(self, index):
        slot = self.slot_of(IRVINE)
        if slot is None:
            return
        mem = self.mem
        self.drop("irvine")
        shot, ammo = SHOTS[index]
        mem.remember(SAVE_IRVINE_SHOTS)

        def put_back():
            mem.restore(SAVE_IRVINE_SHOTS)

        mem.write_u8(CB_CRISIS + slot * CB_STRIDE, CRISIS_MAX)
        info = self.info(K_SHOT, 24, index, 0x0A)
        if not mem.write_pending(slot, CMD_SHOT, AMMO_FIRST_ITEM + index,
                                 default_target(mem, info, slot)):
            put_back()
            return
        self.log(f"Queued {shot} for Irvine (slot {slot}). Press R1 to fire.")
        started = [False]

        def step():
            on = mem.u8(G_SHOT_ON)
            if on is None:
                put_back()
                return True
            if not started[0]:
                if on == 1 and mem.u8(G_SHOT_TYPE) == index:
                    started[0] = True
                    if not mem.u8(G_SHOT_LEFT):
                        mem.write_u8(G_SHOT_LEFT, NO_AMMO_SHOTS)
                        self.log(f"No {ammo} in the bag, so it gets {NO_AMMO_SHOTS} shots.")
                return False
            if on == 0:
                put_back()
                return True
            return False

        def on_timeout():
            if not started[0]:
                self.log("The Shot didn't start. Was it Irvine's turn?")

        self.watch("irvine", step, 90, put_back, on_timeout)

    # ---- Quistis
    def blue_magic(self, index):
        self.queue(QUISTIS, CMD_BLUE_MAGIC, index, self.info(K_BLUE_MAGIC, 16, index, 0x09),
                   BLUE_MAGIC[index])

    # ---- Rinoa
    def combine(self, index):
        self.queue(RINOA, CMD_COMBINE, 0, self.info(K_COMBINE, 20, index, 0x08),
                   f"Combine: {COMBINE[index]}", extra1=index)

    def angel_wing(self):
        self.queue(RINOA, CMD_COMBINE, 1, self.info(K_COMBINE, 20, ANGEL_WING, 0x08),
                   "Angel Wing", extra1=ANGEL_WING)

    def angelo(self, attack, name, on_ally):
        slot = self.slot_of(RINOA)
        if slot is None:
            return
        info = 0x10 if on_ally else 0x50          # one ally (Rinoa) / one enemy
        if self.mem.write_pending(slot, CMD_NJGF, attack, default_target(self.mem, info, slot)):
            self.log(f"Queued {name} for Rinoa (slot {slot}).")

    # ---- Selphie
    def slot_spell(self, magic_id, name):
        slot = self.slot_of(SELPHIE)
        if slot is None:
            return
        mem = self.mem
        self.drop("selphie")
        mem.remember(SAVE_SELPHIE_SLOT)
        before = mem.saved.get(SAVE_SELPHIE_SLOT)

        def put_back():
            mem.restore(SAVE_SELPHIE_SLOT)

        mem.write_u8(CB_CRISIS + slot * CB_STRIDE, CRISIS_MAX)
        info = self.info(K_MAGIC, 60, magic_id, 0x0A)
        if not mem.write_pending(slot, CMD_SLOT, magic_id, default_target(mem, info, slot)):
            put_back()
            return
        self.log(f"Queued Slot: {name} for Selphie (slot {slot}).")

        def step():
            now = mem.u8(SAVE_SELPHIE_SLOT)
            if now is None or before is None or now != before[0]:
                put_back()
                return True
            return False

        self.watch("selphie", step, 20, put_back)

    # ---- guests
    def guest(self, char):
        _char, command, index, name = next(g for g in GUESTS if g[0] == char)
        from_kernel = self.mem.u8(K_CHAR + char * 36 + 4)
        if from_kernel in GUEST_COMMANDS:
            command = from_kernel
        self.queue(char, command, index, self.info(K_TEMP_LIMIT, 24, index, 0x0A), name)


# ---------------------------------------------------------------- UI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"FF8 Limit Caller v{APP_VERSION} - by AxlRose")
        self.geometry("640x680")
        self.minsize(600, 620)

        # Set custom icon (icon.ico lives in the _internal bundle folder)
        try:
            base = Path(getattr(sys, "_MEIPASS", get_exe_dir()))
            icon_path = base / "icon.ico"
            if icon_path.exists():
                self.iconbitmap(str(icon_path))
        except Exception:
            pass

        self.mem = Mem(self._log)
        self.caller = LimitCaller(self.mem, self._log)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(TICK_MS, self._tick)
        self.after(1000, self._refresh_party)

    def _build_ui(self):
        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Attach to FF8", command=self._attach).pack(side="left")
        self.status_var = tk.StringVar(value="Not attached.")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=10)

        self.party_var = tk.StringVar(value="Battle party: -")
        ttk.Label(self, textvariable=self.party_var, padding=(8, 0)).pack(fill="x")

        nb = ttk.Notebook(self)
        nb.pack(fill="x", padx=8, pady=8)
        c = self.caller

        tab = self._tab(nb, "Squall", "The finisher buttons make the Renzokuken end with that finisher, "
                                      "even if his gunblade doesn't have it yet. Press R1 on the hits like normal.")
        self._group(tab, "Renzokuken", [("Renzokuken (random finisher)", c.renzokuken)], cols=1, width=30)
        self._group(tab, "Renzokuken ending in",
                    [(n, lambda k=k: c.finisher(k)) for k, n in enumerate(FINISHERS)])

        tab = self._tab(nb, "Zell", "A move button starts a Duel, and Zell does that move after his opening one. "
                                    "While the Duel timer runs, click more moves to chain them. "
                                    "The finishers end the Duel.")
        self._group(tab, "Duel", [("Duel (enter the combos yourself)", c.duel)], cols=1, width=34)
        self._group(tab, "Moves", [(n, lambda m=m: c.duel_move(m))
                                   for m, n in enumerate(DUEL_MOVES) if m < DUEL_FIRST_FINISHER], cols=3)
        self._group(tab, "Finishers", [(n, lambda m=m: c.duel_move(m))
                                       for m, n in enumerate(DUEL_MOVES) if m >= DUEL_FIRST_FINISHER])

        tab = self._tab(nb, "Irvine", "Press R1 to fire like normal. If you don't have that ammo, it still fires.")
        self._group(tab, "Shot", [(n, lambda i=i: c.shot(i)) for i, (n, _ammo) in enumerate(SHOTS)])

        tab = self._tab(nb, "Quistis", "Every Blue Magic, learned or not.")
        self._group(tab, "Blue Magic", [(n, lambda i=i: c.blue_magic(i)) for i, n in enumerate(BLUE_MAGIC)])

        tab = self._tab(nb, "Rinoa", "Combine moves and Angel Wing, learned or not. "
                                     "The Angelo row is his moves on his own.")
        self._group(tab, "Combine", [(n, lambda i=i: c.combine(i)) for i, n in enumerate(COMBINE)]
                    + [("Angel Wing", c.angel_wing)], cols=3)
        self._group(tab, "Angelo", [(n, lambda a=a, n=n, ally=ally: c.angelo(a, n, ally))
                                    for n, a, ally in ANGELO_SOLO])

        tab = self._tab(nb, "Selphie", "Casts the spell once through Slot, the special ones included.")
        self._group(tab, "Slot specials", [(n, lambda mid=mid, n=n: c.slot_spell(mid, n))
                                           for mid, n in SLOT_SPECIALS])
        box = ttk.LabelFrame(tab, text="Any spell", padding=8)
        box.pack(fill="x")
        self.spell_var = tk.StringVar()
        spells = ttk.Combobox(box, textvariable=self.spell_var, state="readonly", width=26,
                              values=[f"{i + 1:02d}  {n}" for i, n in enumerate(MAGIC_NAMES)])
        spells.current(0)
        spells.pack(side="left")
        ttk.Button(box, text="Cast", command=self._slot_any).pack(side="left", padx=8)

        tab = self._tab(nb, "Guests", "These work when that character is in the party.")
        self._group(tab, "Limit Breaks", [(f"{CHAR_NAMES[g]}: {n}", lambda g=g: c.guest(g))
                                          for g, _cmd, _i, n in GUESTS], cols=2, width=24)

        logf = ttk.LabelFrame(self, text="Log", padding=6)
        logf.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.logbox = tk.Text(logf, height=7, wrap="word")
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")

        ttk.Label(self, padding=(8, 0, 8, 6), foreground="#666",
                  text="Click a limit when that character's command menu is up.").pack(fill="x")

    def _tab(self, nb, title, hint):
        frame = ttk.Frame(nb, padding=8)
        nb.add(frame, text=title)
        ttk.Label(frame, text=hint, foreground="#555", wraplength=580,
                  justify="left").pack(fill="x", pady=(0, 6))
        return frame

    def _group(self, parent, title, items, cols=4, width=16):
        box = ttk.LabelFrame(parent, text=title, padding=8)
        box.pack(fill="x", pady=(0, 8))
        for i, (label, command) in enumerate(items):
            ttk.Button(box, text=label, width=width, command=command).grid(
                row=i // cols, column=i % cols, padx=3, pady=3, sticky="ew")
        for col in range(cols):
            box.columnconfigure(col, weight=1)
        return box

    # ---- helpers
    def _log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _attach(self):
        self.caller.drop()
        if self.mem.attached():
            self.mem.detach()
        if self.mem.attach():
            self.status_var.set(f"Attached: {self.mem.name}")
        else:
            self.status_var.set("Not attached (see log).")
        self._show_party()

    def _show_party(self):
        if not self.mem.attached():
            self.party_var.set("Battle party: -")
            return
        if self.mem.read(PENDING_BUFFER, 1) is None:          # the game was closed
            self.caller.drop()
            self.mem.detach()
            self.status_var.set("Not attached (FF8 closed?).")
            self.party_var.set("Battle party: -")
            return
        members = party(self.mem)
        if any(m is not None for m in members):
            self.party_var.set("Battle party:   " + "     ".join(
                f"Slot {i}: {CHAR_NAMES[m] if m is not None else '-'}" for i, m in enumerate(members)))
        else:
            self.party_var.set("Battle party: none found (get into a battle).")

    def _refresh_party(self):
        self._show_party()
        self.after(1000, self._refresh_party)

    def _tick(self):
        self.caller.tick()
        self.after(TICK_MS, self._tick)

    def _slot_any(self):
        try:
            magic_id = int(self.spell_var.get().split()[0])
        except (ValueError, IndexError):
            return
        self.caller.slot_spell(magic_id, MAGIC_NAMES[magic_id - 1])

    def _on_close(self):
        self.caller.drop()
        self.mem.restore_all()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
