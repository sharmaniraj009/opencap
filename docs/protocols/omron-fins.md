# Omron FINS protocol notes (CS/CJ series, FINS/UDP and FINS/TCP)

These notes cover the read subset opencap's FINS driver needs
([#54](https://github.com/sharmaniraj009/opencap/issues/54), Rust-first per ADR-0002). They are
also what the test simulator `tests/helpers/fins_sim.py` implements
([#13](https://github.com/sharmaniraj009/opencap/issues/13)). The address vectors are in
[`tests/conformance/omron_fins/`](../../tests/conformance/omron_fins/).

opencap is read-only (ADR-0008). The driver frames only three commands: MEMORY AREA READ (0101),
MULTIPLE MEMORY AREA READ (0104) and CPU UNIT DATA READ (0501). It never frames a write, fill,
transfer, run/stop, clock, access-right or forced set/reset command.

## Sources

Sections are cited as **[W342 5-2-2]** and so on. Only short phrases are quoted; everything
else is paraphrased.

| Ref | Document | Copy used |
|---|---|---|
| **W342** | Omron W342-E1-15, *CS/CJ/CP/NSJ Series Communications Commands Reference Manual* | [mirror](https://www.farnell.com/datasheets/1726171.pdf) |
| **W421** | Omron W421-E1-03, *CS1W-ETN21/CJ1W-ETN21 Ethernet Units Construction of Applications* | [mirror](https://archive.org/download/manualsonline-id-3178efcf-a594-4da9-b947-a945f757c868/3178efcf-a594-4da9-b947-a945f757c868.pdf) |
| **W420** | Omron W420, *CS1W-ETN21/CJ1W-ETN21 Ethernet Units Construction of Networks* | [mirror, §3-1 and §5-2](https://www.manualsdir.com/manuals/203937/omron-cs1w-etn21.html?page=87) |
| **PTC** | PTC *Omron FINS Ethernet Driver* manual v1.083 (KEPServerEX / TOP Server): address syntax, data types, device properties | [copy](https://downloads.softwaretoolbox.com/demodnld/prod_docs/topserver_help_pdf/common/omron-fins-ethernet-manual.pdf) |

Some facts are marked *(sim choice)*. The manuals leave these open, and the simulator does what
is listed here. None has been confirmed on hardware, so check them when the driver is
commissioned (ADR-0014 §5).

## 1. Transport

| | FINS/UDP | FINS/TCP |
|---|---|---|
| Port | 9600 by default (unit setting) [W421 7-4] | 9600 by default [W421 7-4] |
| Message | one FINS frame per datagram | 16-byte FINS/TCP header, then the payload |
| Addressing | the client picks its own source node (SA1); the unit maps nodes to IP addresses | the unit assigns the client node during a handshake |
| Retries | done by the client (the core); replies are matched by SID | TCP delivers; a request can still time out |
| Max FINS frame | 12 to 2,012 bytes (header + command + data) [W421 7-2] | the same FINS frame, carried in the TCP payload |
| KEPServerEX | **supported** (UDP only) [PTC p.4] | **not supported**: opencap adds it in #54 |

### 1.1 FINS header (10 bytes, every frame) [W342 3-3-3, W421 7-2]

| Byte | Field | Command value | Response |
|---|---|---|---|
| 0 | ICF | `0x80`: bit 7 always 1, bit 6 = 0 (command), bit 0 = 0 (response required) | bit 6 set (`0xC0`) |
| 1 | RSV | `0x00` | `0x00` |
| 2 | GCT | gateway count: `0x02`, or `0x07` for 8-level networks [W342 3-3-3] | not checked |
| 3 | DNA | destination network (0 = local network) | the request's SNA |
| 4 | DA1 | destination node (the CPU's Ethernet unit) | the request's SA1 |
| 5 | DA2 | destination unit (0 = CPU unit) | the request's SA2 |
| 6 | SNA | source network | the request's DNA |
| 7 | SA1 | source node (the client) | the CPU's node |
| 8 | SA2 | source unit (0) | the request's DA2 |
| 9 | SID | service ID, chosen by the client | echoed |

The command code (MRC, SRC; 2 bytes) follows the header, then the command data. A response
repeats the command code, then has a 2-byte **end code** (MRES, SRES; §4), then the response data.

A client matches each reply to its request by SID and command code, and drops any reply whose
SID does not match. On UDP, a late reply to a request that already timed out would otherwise be
taken as the reply to the next request.

### 1.2 FINS/UDP

The Ethernet unit converts between FINS node numbers and IP addresses. It has three methods:
automatic generation (dynamic or static), an IP address table, or a combination. The default is
automatic, dynamic [W420 3-1]. The driver has to get three things right:

- **DA1 is the CPU's node number.** Under automatic generation this is the host part of the
  unit's IP address. KEPServerEX defaults DA1 to the last octet of the device IP [PTC p.12].
  The simulator does not answer frames whose DA1 is not its node: it counts them in
  `misaddressed` (`strict_node`) *(sim choice: the driver sees a timeout, the worst case)*.
- **SA1 must be a node the unit can answer.** KEPServerEX defaults SA1 to the last octet of the
  host's adapter address, and it must be unique per channel [PTC p.11–12].
- **The reply port depends on the method.** Under the dynamic method, or with the IP table or
  combined method when the host is not registered, the unit replies to the datagram's source
  port [W421 7-3-1]. Under the static method, or for a host that is registered in the table, it
  replies to the unit's own FINS port (9600) [W420 5-2]. KEPServerEX therefore requires
  "source and destination port number be the same" [PTC p.7] and binds local port 9600.
  Recommendation for #54: bind an ephemeral port by default (this works with the default
  dynamic method), and add a device setting that binds the FINS port instead, for units in
  static or table mode. The simulator replies to the source address.

### 1.3 FINS/TCP [W421 7-4]

Every message starts with a 16-byte header. All fields are big-endian:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `"FINS"` (`46 49 4E 53`) |
| 4 | 4 | length: the number of bytes from the command field onwards (8 + payload) |
| 8 | 4 | command |
| 12 | 4 | error code |

| Command | Direction | Payload |
|---|---|---|
| 0 | client → unit: node address data send | the client node (4 bytes); 0 means "allocate one for me" |
| 1 | unit → client: node address data send | the client node (4) + the server node (4) |
| 2 | both ways: FINS frame send | one FINS frame (§1.1); length `0x14`–`0x7E4` |
| 3 | unit → client: FINS frame send error notification | — |
| 6 | connection confirmation | — (not used by the driver or the simulator) |

The handshake comes first on every connection. The client sends command 0 with its node number,
or 0. The unit answers with command 1, carrying the client node it accepted or allocated, plus
its own node. By default the unit allocates from **239 to 254** and accepts up to **16**
connections. After the handshake, every request and response is a command 2 message, and the
FINS frame inside it uses SA1 = the allocated client node and DA1 = the server node.

Error codes in the header's error field. After an error the connection is closed. Codes
`0x20`–`0x25` arrive only in command 1:

| Code | Meaning | Simulator |
|---|---|---|
| `0x01` | the header is not `"FINS"` | answers command 3 and closes |
| `0x02` | the data length is too long | answers command 3 and closes |
| `0x03` | the command is not supported (this includes a FINS frame before the handshake, and a second handshake) | answers command 3 and closes |
| `0x20` | all connections are in use | answers command 1 and closes (17th client) |
| `0x21` | the requested client node is already connected | answers command 1 and closes |
| `0x22` | a protected node was accessed from an unregistered IP address | — |
| `0x23` | the client node is out of range | answers command 1 and closes |
| `0x24` | the client node equals the server's node | answers command 1 and closes |
| `0x25` | no node is left to allocate | answers command 1 and closes |

## 2. Commands

### 2.1 MEMORY AREA READ (`01 01`) [W342 5-3-2]

| Request data | Size |
|---|---|
| memory area code | 1 |
| beginning address: word (2 bytes, big-endian) + bit (1 byte; `00` for word access) | 3 |
| number of items | 2 |

The response data is the items, `bytes per element × count` long. A word is 2 bytes, big-endian.
A bit, completion flag or task flag is 1 byte (`00` or `01`). An index register is 4 bytes. A
bit read starts at word.bit and continues across word boundaries (`CIO10.15`, then `CIO11.00`).
A count of 0 is a normal completion that returns no data [W342 5-3-2 note 1].

**Maximum items:** 999 words per read over Ethernet [W342 5-2-2]. That is the 2,012-byte frame
minus 14 bytes of header, command code and end code, divided by 2. The planner caps blocks at
`min(999, KEPServerEX Request Size / 2)` words. KEPServerEX's "Request Size" setting is 32, 64,
128, 256, 512, 1024 or 1984 bytes, default 512, so the default block is 256 words [PTC p.13].

### 2.2 MULTIPLE MEMORY AREA READ (`01 04`) [W342 5-3-5]

The request repeats, per item: memory area code (1 byte) + beginning address (3 bytes). The
response repeats, per item: the memory area code (1 byte), then the value in that area's element
size (bit 1 byte, word 2 bytes, IR 4 bytes). The limit is **167 items** over Ethernet or
Controller Link, and 89 over SYSMAC LINK or DeviceNet. 0104 suits scattered tags, where one
0101 per tag would cost more requests than one 0104.

When any item fails, the simulator answers the whole command with that item's end code and no
data *(sim choice)*.

### 2.3 CPU UNIT DATA READ (`05 01`) for identity [W342 5-3-15]

| Request data | Response data |
|---|---|
| `00` | CPU unit model (20 bytes ASCII, space padded) + internal system version (20 bytes ASCII, null padded) + system use (40 bytes; byte 1 = DIP switches, byte 2 = largest EM bank) + area data (12 bytes): 92 bytes |
| `01` | CPU Bus Unit configuration (64) + remote I/O data (2) + CPU Unit information (1): 67 bytes |
| none | both of the above: 159 bytes |

Design for #54: the driver sends `00`, reports the model and version as device identity, and
logs them once per connection. If the CPU answers 0501 with an error, identity stays empty and
reads go on.

### 2.4 Commands the driver never frames

W342 5-1-1 marks these as writing to or changing the CPU: `0102` MEMORY AREA WRITE, `0103`
MEMORY AREA FILL, `0105` MEMORY AREA TRANSFER, `0202`/`0203` parameter area write/clear,
`0307`/`0308` program area write/clear, `0401`/`0402` RUN/STOP, `0702` CLOCK WRITE, `0C01`/`0C02`
access right acquire, `2101`/`2103`/`2141` error and log clear, the file memory writes
`2203`–`220C`, `2215` and `2220`, `2301`/`2302` FORCED SET/RESET (and cancel), and the serial
gateway passthroughs `2803`–`2805`.

The simulator refuses every one of them (and the CV-mode `0304`/`0305`/`0403`) with end code
`0x0401`, counts them in `write_attempts`, and leaves memory unchanged *(sim choice; a real CPU
executes them)*. #54's acceptance requires a unit test proving the driver's frame builder cannot
produce any of them.

## 3. Memory areas (CS/CJ mode) [W342 5-2-2]

| Area | KEPServerEX | Word code | Bit / flag code | FINS addresses | KEPServerEX range [PTC p.57–70] |
|---|---|---|---|---|---|
| CIO | `CIO` | `B0` | `30` | word *n* | CIO0000–6143 |
| Work | `W` | `B1` | `31` | word *n* | W000–511 |
| Holding | `H` | `B2` | `32` | word *n* | H0000–1535 (H512–1535: function block area) |
| Auxiliary | `A` | `B3` | `33` | word *n* | A000–959; CJ2: A000–1471 and A10000–11535 |
| DM | `D` | `82` | `02` | word *n* | D00000–32767 |
| EM bank 0–F hex | `E00:`…`E15:` | `A0`–`AF` (CJ2 also `50`–`5F`) | `20`–`2F` | word *n* | banks 0–12 on CS1/CJ1, 0–24 on CJ2 (decimal in KEPServerEX) |
| EM bank 10–18 hex (CJ2) | `E16:`…`E24:` | `60`–`68` | `E0`–`E8` | word *n* | |
| EM current bank | `E` | `98` | `0A` | word *n* | E00000–32767 |
| EM current bank number | — | `BC` | — | `0F00` | the driver may read it to report which bank `E<n>` refers to |
| Timer PV / completion flag | `T` / `TS` | `89` | `09` | timer *n* = `0000`+*n* | T0000–4095 / TS0000–4095 |
| Counter PV / completion flag | `C` / `CS` | `89` | `09` | counter *n* = `8000`+*n* | C0000–4095 / CS0000–4095 |
| Task flag | `TK` | — | `06` (status `46`) | flag *n* | TK00–31; CJ2: TK00–127 |
| Index register | `IR` | `DC` (4 bytes per element) | — | `0100`+*n* | IR00–15 |
| Data register | `DR` | `BC` | — | `0200`+*n* | DR00–15 |

A FINS address is the word (2 bytes, big-endian) followed by the bit number: 0–15 for bit
access, `00` otherwise. Flags (timers, counters, task flags) take one element per flag, with
the bit field `00`.

C-mode (C200H and others) and CV-mode CPUs use different codes. For example, in CV mode CIO and
AR words are `80`, timer PV is `81`, the EM banks are `90`–`97` and DR is `9C` [W342 5-2-2].
KEPServerEX supports those CPUs as other models. opencap's first driver covers CS1, CJ1 and CJ2
and refuses any other model when the config is loaded.

### 3.1 Value encoding

- A word is big-endian on the wire.
- **32-bit values:** Omron CPUs store double-word operands with the low word at the lower
  address. The simulator's default (`word_order="low_first"`) follows that. The PTC manual does
  not state KEPServerEX's word order for this driver. It only says IR values are "native 32-bit
  values with MSB to LSB byte ordering". #54 keeps a `first_word_low` device parameter (default
  `true`), and confirming KEPServerEX's behaviour is a commissioning check.
- **IR** (index registers) arrive as one 4-byte element, most significant byte first.
- **BCD:** KEPServerEX's BCD type (`bcd16`, 0–9999) and LBCD type (`bcd32`, 0–99999999, low word
  first). The `D` suffix (`D0D`) reads a Word or DWord as unsigned BCD, and a Short or Long as
  *signed* BCD, with the most significant bit as the sign. The PTC manual's example is
  `D0D @ Short = -50`, which is the raw word `8050`. The suffix is not allowed on Boolean,
  Float, String, BCD or LBCD tags, nor on timers and counters, whose default type is already BCD.
- **Strings:** the length goes where the bit number would be. `H` and `L` strings hold two
  characters per word (high byte first or low byte first), with an even length from 2 to 512.
  `D` and `E` strings hold one character per word (in the high or low byte), with a length from
  1 to 256 [PTC p.65, examples `D01000.100H`, `D01100.078L`, `D02000.055D`, `D02200.037E`].

## 4. End codes [W342 5-1-3]

The end code is MRES then SRES. Mask before classifying (`code & 0x7F3F`):

- MRES bit 7 flags a network relay error. A relay error word (network, node) follows the end code.
- SRES bit 7 flags a **fatal** CPU error and SRES bit 6 a **non-fatal** one. Both come *in
  addition to* the command's own result. With a battery error, for example, a normal completion
  reads `0040`. Data returned with a masked end code of `0000` is valid, and the driver reports
  the CPU error flag in its diagnostics.

| End code (masked) | W342 meaning | Driver error | Why |
|---|---|---|---|
| `0000` | normal completion | — | |
| `0001` | service cancelled | `CommError` | transient |
| `0101`–`0106` | local node errors (not in the network, token, retries, too many sends, node address) | `CommError` | network state |
| `0201`–`0205` | destination node errors (not in the network, unit missing, third node missing, busy, response timeout) | `CommError` | transient, or a device-level setting |
| `0301`–`0304` | controller errors (communications controller, CPU unit, no response, unit number) | `CommError` | the device goes bad_comm |
| `0401` | undefined command | `BlockConfigError` | the CPU lacks the command; on 0104 the planner falls back to 0101 |
| `0402` | unit model or version does not support the command | `BlockConfigError` | as above |
| `0501`–`0504` | routing table errors | `CommError` | network configuration at device level, not this block |
| `1001`–`1004` | command too long, too short, elements/data mismatch, bad format | `BlockConfigError` | a planner bug: never retry the same block |
| `1005` | header error (relay or routing table) | `CommError` | network configuration |
| `1101` | area classification missing: "the specified word does not exist in the memory area or there is no EM Area" | `BlockConfigError` | the address does not exist on this CPU |
| `1102` | access size wrong | `BlockConfigError` | |
| `1103` | address range designation error: the start address is outside the area | `BlockConfigError` | |
| `1104` | address range exceeded: the end address is outside the area | `BlockConfigError` | |
| `1109`, `110B`, `110C` | relational error; response too long; parameter error | `BlockConfigError` | |
| `20xx`, `21xx` | read/write not possible (protected, table missing, read-only, …) | `BlockConfigError` | not expected for reads |
| `22xx` | wrong CPU mode | `CommError` | transient |
| `2301` | the specified memory does not exist (for example EM formatted as file memory) | `BlockConfigError` | |
| `2302`, `2303` | no file memory; no clock | `BlockConfigError` | not expected for reads |
| `24xx`, `25xx` | data link table; unit errors (memory, I/O, bus) | `CommError` | the device goes bad_comm |
| `26xx` | command errors (protection, service running) | `CommError` | |
| `3001` | access right held by another device | `CommError` | transient |
| `4001` | aborted | `CommError` | transient |
| anything else | | `CommError` | retry, then bad_comm |

A missing reply, or a reply shorter than the items requested, is a `CommError`.

The simulator answers faults as follows:

| Fault | End code |
|---|---|
| unknown area code, or EM bank not fitted | `1101` |
| first address outside the area, or a bit number above 15 | `1103` |
| a word or flag read with a non-zero bit field | `1103` *(sim choice)* |
| a read that runs past the end of the area | `1104` |
| more than 999 words in one read | `110B` *(sim choice)* |
| request data too short | `1002` |
| request data too long | `1001` |
| a command it does not implement | `0401` |

## 5. KEPServerEX address syntax (CS1/CJ1/CJ2) [PTC p.57–70]

The PTC manual has one addressing table per model: **CS1** on pp. 57–61, **CJ1** on pp. 62–66
and **CJ2** on pp. 66–70. The EM rows are the source for the `E<bank>:<word>` spelling (#166):

- "Expansion Data Memory" is written `E00:00000-E12:32767` on CS1 (p. 59) and CJ1 (p. 64), and
  `E00:00000-E24:32767` on CJ2 (p. 68).
- "Expansion Data Memory (current bank)" is `E00000-E32767` on the same pages.
- The bank number is decimal: CS1/CJ1 have 13 banks (0–C hex), CJ2 has 25 (0–18 hex).

Omron's own manuals write the banked form as `E0_00000` [W342 5-2-2]; KEPServerEX does not
accept that spelling.

| Address | Meaning | Default type |
|---|---|---|
| `CIO<w>`, `CIO<w>.<bb>` | CIO word or bit (`CIO10.05` = word 10, bit 5) | Word / Boolean |
| `W<w>`, `H<w>`, `A<w>`, `D<w>` (+ `.<bb>`) | work, holding, auxiliary, DM | Word / Boolean |
| `E<bank>:<w>`, `E<bank>:<w>.<bb>` | EM bank, decimal bank number (`E00:00100`, `E12:32767`; CJ2 up to `E24:`) | Word / Boolean |
| `E<w>`, `E<w>.<bb>` | EM current bank (`E00100`) | Word / Boolean |
| `T<n>`, `C<n>` | timer / counter present value | **BCD** |
| `TS<n>`, `CS<n>` | timer / counter completion flag | Boolean |
| `TK<n>` | task flag (read-only) | Boolean |
| `IR<n>` | index register (32-bit only) | **DWord** |
| `DR<n>` | data register | Word |
| `<word address>.<len>H` / `L` / `D` / `E` | string (§3.1) | String |
| `<word address>D` | BCD suffix (§3.1) | |
| `<address>[cols]`, `<address>[rows][cols]` | array (not Boolean or String) | |

The data types are Boolean, Short, Word, Long, DWord, Float, BCD, LBCD and String. They map to
opencap's `bool`, `int16`, `uint16`, `int32`, `uint32`, `float32`, `bcd16`, `bcd32` and `string`.
KEPServerEX has no 64-bit types for this driver. A 32-bit type needs two words in range, so a
DWord can start at D32766 but not at D32767. Leading zeros are accepted (`D00100` is `D100`).

Two spellings in circulation are **not** KEPServerEX syntax:

- `E0_100`: this is Omron's own notation for EM bank 0, word 100 [W342 5-2-2]. KEPServerEX
  writes `E00:00100`.
- `EM100`: KEPServerEX writes the current bank as `E00100`.

The vectors list both as invalid. An importer must not accept them silently.

## 6. Test harness

`tests/helpers/fins_sim.py` (`FinsSim`) serves FINS/UDP and FINS/TCP on one ephemeral port, with
the CS/CJ memory areas in §3, the three commands in §2 and the end codes in §4.

- **Fault switches:** `mute`, `end_code`, `area_end_codes`, `cpu_data_end_code`,
  `cpu_error_bits`, `short_reply`, `sid_offset`, `reply_delay`, `split_replies`, `strict_node`,
  and `restart()` on the same port with the same memory.
- **Counters:** `request_count`, `command_counts`, `write_attempts`, `error_replies`,
  `misaddressed`, `tcp_errors`, `handshakes`, `last_header`.
- **Values:** `set()` and `value()` take KEPServerEX addresses and use the simulator's own
  encoder and parser, never a driver's.

`tests/unit/test_fins_sim.py` validates the simulator in two independent ways:

1. **Hand-built frames.** The test writes byte literals from the layouts above, with no
   simulator code, and sends them over raw UDP and TCP sockets. They cover memory area read
   (one golden request/response pair), bit reads, every area code, EM banks (including CJ2
   banks and the current bank), timers and counters, task flags, index and data registers, the
   current bank number, multiple memory area read, CPU unit data read (all three forms), every
   end code, the CPU error bits, the FINS/TCP handshake and its errors, split TCP replies, and
   two TCP clients.
2. **fins-driver 0.3.1**, unmodified (MIT,
   [LICENSE at v0.3.1](https://github.com/indrarudianto/fins-driver/blob/v0.3.1/LICENSE)), over
   FINS/UDP. It checks word, bit, BCD, EM and multiple-area reads, end codes, and the write
   commands it offers (refused and counted). Its `mode="tcp"` sends bare FINS frames on a TCP
   socket, with no FINS/TCP header or handshake, and no Ethernet unit accepts that. The
   simulator answers error 1 and closes, and the test pins this behaviour.

The address vectors are in conformance format 1, area `omron_fins/addresses`
(`tests/conformance/omron_fins/addresses/`, generated by `scripts/gen_conformance.py` from
`scripts/gen_conformance_fins.py`). Their expected values come from the simulator's own
parser, and must equal the hand-written expectations in the case module (#166). The Rust driver (#54) must pass the same vectors, and the simulator suite with
`OPENCAP_ENGINE=rust`.
