# The protocols as Probel Pangolin speaks them

SW-P-08 and SW-P-02 are the two router control protocols of Pro-Bel, later
Snell, now Grass Valley. This page says how a message looks on the wire, which
messages the program knows, and where each of these facts comes from.

- [SW-P-08, the General Remote Control Protocol](#sw-p-08-the-general-remote-control-protocol)
- [SW-P-02, the General Switcher Communication Protocol](#sw-p-02-the-general-switcher-communication-protocol)
- [Where the protocol comes from](#where-the-protocol-comes-from)
- [Not covered](#not-covered)

All numbers in the program count from 1, as on a panel. On the wire they
count from 0. The byte tables below show the wire.

## SW-P-08, the General Remote Control Protocol

A controller and a router exchange messages over TCP, on a port set at the
router (8910 is a common choice), or over RS-232 and RS-422. Every message is
wrapped into a frame: `DLE STX`, the data bytes, a byte count, a checksum,
`DLE ETX`, and every data byte that equals DLE (0x10) is sent twice. The
receiver answers each frame with `DLE ACK` or, if count or checksum are wrong,
with `DLE NAK`, within one second and best within 10 milliseconds. A message
without ACK is sent again, up to five times. A crosspoint is named by matrix,
level, destination and source. The standard commands reach 16 matrices, 16
levels and 1024 sources and destinations, the extended commands 256 matrices,
256 levels and 65536 sources and destinations.

All numbers in this program count from 1, as on a panel. On the wire they
count from 0.

### The frame

```
DLE STX | DATA ............ | BTC | CHK | DLE ETX
10  02  | command, message  |     |     | 10  03
```

| Part | Meaning |
|---|---|
| `DLE STX` | Start of message, `10 02` |
| DATA | The command byte and the message bytes. Up to 128 bytes work with every system, up to 255 in custom applications |
| BTC | Number of DATA bytes |
| CHK | Two's complement of the sum of DATA and BTC, in 8 bits: all of them and CHK add up to 0 |
| `DLE ETX` | End of message, `10 03` |

Every byte `10` inside DATA, BTC and CHK is sent twice, so that it is not
taken for a control byte. The receiver answers `10 06` (ACK) for a frame with
correct count and checksum and `10 15` (NAK) otherwise. An ACK says the frame
arrived intact, not that the router did what was asked. An ACK or NAK may
arrive in the middle of a frame that is still being received.

Take source 5 to destination 3 on level 2:

```
10 02  02 01 00 02 04  05 F2  10 03
       |  |  |  |  |   |  checksum
       |  |  |  |  |   byte count: 5
       |  |  |  |  source 5      (wire 4)
       |  |  |  destination 3    (wire 2)
       |  |  multiplier
       |  matrix 1, level 2      (wire 0 and 1)
       command 02: crosspoint connect
```

The router answers `10 06`, makes the route and tells every controller:
`10 02 04 01 00 02 04 05 F0 10 03`, crosspoint connected.

### The standard commands

Matrix and level share one byte: matrix in the upper four bits, level in the
lower four. Destination and source have seven bits each in their own byte,
and their upper three bits sit in the multiplier byte:

| Bits of the multiplier | Meaning |
|---|---|
| 7 | 0 |
| 4 to 6 | Destination divided by 128 |
| 3 | The source is bad (TDM and digital video routers), in tally and connected |
| 0 to 2 | Source divided by 128 |

| Command | Name | Direction | Bytes after the command |
|---|---|---|---|
| `01` | Crosspoint interrogate | to router | matrix/level, multiplier, destination |
| `02` | Crosspoint connect | to router | matrix/level, multiplier, destination, source |
| `03` | Crosspoint tally | from router | as connect. The answer to an interrogate |
| `04` | Crosspoint connected | from router, to every controller | as connect. Sent after a route was made |
| `08` | Dual controller status request | to router | none |
| `09` | Dual controller status response | from router | status. Bit 1 of the first byte tells active from idle |
| `15` | Crosspoint tally dump request | to router | matrix/level |
| `16` | Crosspoint tally dump (byte) | from router | matrix/level, number of tallies, first destination, one source byte each. Up to 191 sources and destinations |
| `17` | Crosspoint tally dump (word) | from router | matrix/level, number of tallies (at most 64), first destination in two bytes, two source bytes each |
| `61` | Protocol implementation request | to router | none |
| `62` | Protocol implementation response | from router | how many commands the router sends, how many it accepts, then the two lists |
| `64` | All source names request | to router | matrix/level, name length (0, 1, 2 for 4, 8, 12 characters) |
| `66` | All destination names request | to router | matrix in the upper four bits, name length |
| `6A` | Source names response | from router | matrix/level, name length, first number in two bytes, number of names, the names |
| `6B` | Destination names response | from router | matrix, name length, first number in two bytes, number of names, the names |
| `78` | Connect on go group salvo | to router | matrix/level, multiplier, destination, source, salvo group (0 to 127) |
| `79` | Go group salvo | to router | 00 set or 01 clear, salvo group |
| `7A` | Connect on go group salvo acknowledge | from router | as `78` |
| `7B` | Go done group salvo acknowledge | from router | 00 set, 01 cleared or 02 nothing stored, salvo group |
| `7C` | Salvo group interrogate | to router | salvo group, index from 0 |
| `7D` | Group salvo tally | from router | as `78`, then index and validity: 00 more follow, 01 the last one, 02 no data |

The command bytes are written in hex here, as the trace shows them. The
specification writes them in decimal: `15` is command 21, `78` is 120.

A long answer comes in several messages: a tally dump in parts of 64, names
in parts of 32, 16 or 10 for 4, 8 or 12 characters. There is no end marker.
The program takes an answer as complete when nothing more arrives for 0.3
seconds.

A salvo sets its crosspoints without a Crosspoint Connected for each. Every
controller keeps its picture right from the acknowledges `7A` and `7B`.

### The extended commands

For more than 16 matrices, 16 levels or 1024 sources and destinations, each
command has an extended form with the top bit set. Matrix and level have a
byte each, destination and source two bytes each, most significant first:

| Command | Name | Bytes after the command |
|---|---|---|
| `81` | Extended crosspoint interrogate | matrix, level, destination (2) |
| `82` | Extended crosspoint connect | matrix, level, destination (2), source (2) |
| `83` | Extended crosspoint tally | as `82` |
| `84` | Extended crosspoint connected | as `82` |
| `95` | Extended tally dump request | matrix, level |
| `97` | Extended tally dump (word) | matrix, level, number of tallies, first destination (2), sources (2 each) |
| `E4` | Extended source names request | matrix, level, name length |
| `E6` | Extended destination names request | matrix, name length |
| `EA` | Extended source names response | matrix, level, name length, first number (2), number of names, the names |
| `EB` | Extended destination names response | matrix, name length, first number (2), number of names, the names |

With **Extended commands** on automatic the program uses them when the router
lists them or when a number does not fit the standard form.

### Timing

A message without ACK after one second is sent again. The specification
recommends five attempts, and that is the default of **Attempts**. It asks a
receiver to acknowledge within 10 milliseconds where possible: the counter
"slower than 10 ms" above the trace shows how a router does. After 30 seconds
of silence the program sends an interrogate for destination 1 to see whether
the router is still there.

## SW-P-02, the General Switcher Communication Protocol

SW-P-02, the General Switcher Communication Protocol, is the older and
simpler sibling. Choose it under **Protocol** in the Controller tab, the
Router tab and the Analyzer. What changes with it:

| | SW-P-08 | SW-P-02 |
|---|---|---|
| Message | `DLE STX`, data, byte count, checksum, `DLE ETX` | `FF`, command, message, 7-bit checksum. The command says how long the message is |
| Acknowledge | `DLE ACK` or `DLE NAK` for every frame | none. The CONNECTED or TALLY of the router is the confirmation |
| Addressing | matrix, level, destination, source | destination and source only: one level |
| Size | 1024, with extended commands 65536 | 1024 destinations, 1023 sources |
| Whole state | tally dump | none. The program asks the destinations one by one |
| Names, implemented commands | yes | no |
| Stored takes | 128 numbered salvo groups | one store: CONNECT ON GO, then GO |
| After a stored set | acknowledges tell every controller | no message. The program asks the destinations again |
| Usual TCP port | set at the router, often 8910 | 2000 |

### The message

```
FF | COMMAND | MESSAGE ...... | CHECKSUM
```

| Part | Meaning |
|---|---|
| `FF` | Start of message |
| COMMAND | The command byte. It also defines how long the message is |
| MESSAGE | Zero or more bytes, each with the top bit 0 |
| CHECKSUM | Two's complement of the sum of command and message, in 7 bits: the top bit is 0 |

Take source 5 to destination 3: `FF 02 00 02 04 78`. The router answers with
CONNECTED on all its ports: `FF 04 00 02 04 76`.

| Command | Name | Direction | Bytes after the command |
|---|---|---|---|
| `01` | Interrogate | to router | multiplier, destination |
| `02` | Connect | to router | multiplier, destination, source |
| `03` | Tally | from router | multiplier, destination, source. Source 1023 on the wire says: this destination is out of range |
| `04` | Connected | from router, on all ports | multiplier, destination, source |
| `05` | Connect on go | to router | multiplier, destination, source |
| `06` | Go | to router | 00 set or 01 clear what was stored |
| `0C` | Connect on go acknowledge | from router | multiplier, destination, source |
| `0D` | Go done acknowledge | from router | 00 set, 01 cleared, 02 nothing stored |

The multiplier byte is the one of SW-P-08. Its bit 3 means a bad source or,
depending on the router, that updating the crosspoint is disabled.

There is no acknowledge. A controller takes a command as done when the
CONNECTED arrives, and takes a missing TALLY as a lost link. After a GO the
router sends no CONNECTED: the controller has to ask. The program does that
for the destinations it stored, and a second controller that only saw the GO
DONE asks all destinations it knows.

Over IP the usual TCP port is 2000, a controller serves up to 8 clients, and
the specification recommends that a client waits at least 100 milliseconds
for an answer. The program waits one second.

## Where the protocol comes from

Two sources, and the program follows each for what it covers:

- **SW-P-88 Router Control Protocols** (Pro-Bel, later Snell, issue 4a of
  2013), the public specification. It defines the frame, ACK and NAK with their
  timing, and the main commands: crosspoint interrogate, connect, tally and
  connected, the tally dump in byte and word format, the group salvo commands,
  and for routers with two controllers the dual controller status. Its three
  examples and its layouts are part of the tests.
- The same document, chapters 1 to 5, for **SW-P-02**: the message format and
  the commands interrogate, connect, tally, connected, connect on go, go and
  their two acknowledges, and SW-P-02 over IP with its port 2000.
- The Bitfocus Companion module
  [generic-swp08](https://github.com/bitfocus/companion-module-generic-swp08)
  for what the public document leaves out: the extended commands for large
  routers and the source and destination names.

What follows from the specification and is easy to miss:

- An ACK may arrive in the middle of a frame. The frame goes on after it.
- In the standard tally messages one bit marks the source as bad (TDM and
  digital video routers). The trace says so.
- The answer to the protocol implementation request starts with two counts:
  how many commands the router sends and how many it accepts.
- 128 data bytes per message are guaranteed to work everywhere, up to 255 only
  in custom applications.
- SYSTEM2 controllers count matrix and level from 1 on the wire, all others
  from 0. For such a controller set Matrix to 2 and add one to the level.
- On HD series routers destinations 897 to 1024 are output monitor rows.
- A real controller serves up to 16 TCP clients.

## Not covered

- Tested against its own virtual router, with the examples of the
  specification and the test vectors of the Companion module, not yet against a
  hardware router.
- Protect commands, UMD labels, tie lines, source association names, "names
  updated" and the extended salvo commands are in neither source in a form that
  could be built on. They show up in trace and analyzer with the name of their
  command and their bytes and can be sent as own messages, but there are no
  buttons for them and the virtual router does not answer them. Protection in
  the virtual router is a switch at the router, not the protect commands of the
  protocol.
- SW-P-02: the extended commands for more than 1024 destinations and the
  status commands are named in the specification but not laid out. They are
  shown as "not decoded". Because an SW-P-02 message carries no length, such a
  message is read up to the next start of message. The UDP multicast with
  which some devices announce changes is not implemented, TCP only.
- Dual controller status: the specification names the bit that tells the
  active from the idle controller, not which value means active. The program
  shows the bit and leaves the reading to you. The virtual router does not
  answer the request.
- A real controller sends a message again, up to five times, when a client
  does not acknowledge it within a second. The virtual router does not.
- TCP only. RS-232 and RS-422 are possible with the library through its own
  connection function, untested.
- The desktop window was built and driven by a script on macOS. Nobody has
  looked at its layout yet, and Windows and Linux have not been run.
