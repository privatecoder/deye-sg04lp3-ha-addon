# First write test: Deye SG04LP3-EU (owner only)

**Only the owner performs this test, on site, with the plant in a state in which an unexpected
value would do no harm.** Agents and automated tests never write to a real device: they use fake
inverters only. This document does not authorise anything by itself; it describes what the app
does (version 0.3.0) and a careful order for the first write. Read `DOCS.md`, “Writing settings”,
first.

Prerequisite: the read test (`docs/LIVE-READ-RUNBOOK.md`) was successful for the same version, all
inverters are `Online` and identified, and you know your plant (which inverter is the parallel
master, which are slaves, whether a GRID charge or a Smart Load circuit is in use).

## 1. What the app can and cannot do

- Nine settings only: maximum battery charge current (R108), discharge current (R109), generator
  charge current (R125), GRID charge current (R128), GRID charge enable (R130 bit 0) and the Smart
  Load thresholds OFF/ON voltage (R134/R136) and OFF/ON SOC (R135/R137). Nothing else can be written,
  not even by accident: the send guard accepts exactly these registers, exactly one register per
  frame, exactly the word you confirmed.
- Every write is one function code 16 frame with one register, sent once. No retry, no fallback to
  another path, no rollback, no automatic restore, no write to other inverters.
- Nothing is written by itself: not at start, not after a reconnect, not on a schedule, not from
  Home Assistant or MQTT. There are no command topics and no writing entities.
- A write is acknowledged and read back **from the inverter's communication processor (MCU1)**.
  This does not prove that the power-stage processor (MCU2) adopted the value, that it survives a
  restart, or that the inverter behaves as intended. You check the display and the effect.
- In a parallel plant it is **not proven** that a value written to the master reaches the slaves,
  nor that a slave keeps its own value. The app therefore writes the one inverter you chose and
  only observes the others (same / different / unavailable). Matching values prove nothing about
  synchronisation.

## 2. Preparation (nothing is written yet)

- [ ] You have the display (LCD) or the cloud app of the inverter at hand and can read the setting
  you are going to change **before** the test: write down the value of the setting on every
  inverter of the plant (display), with date and time. This is your baseline.
- [ ] Decide **one slave** inverter (not the master) as the target.
- [ ] Choose **one inactive, reversible setting**. No address is harmless in general; decide for
  your plant. Candidates, only if the condition holds:
  - GRID charge current (R128): only with an unused GRID input, GRID charge disabled (R130 bit 0
    off) and the stored value inside the allowed range; confirm yourself that changing it has no
    present effect.
  - A Smart Load threshold: only if the GEN port is configured as Smart Load, the load is not
    critical, and you know whether the circuit uses voltage or SOC (check the display; the app
    does not infer it).
  - Otherwise choose another setting or postpone. Do **not** start with the discharge current
    (it can weaken the supply in island operation) or with a value that stops charging (0 A).
- [ ] Never use this test for anything outside the nine settings; there is no way to do so, and no
  other setting is part of it.
- [ ] A restart of the app voids pending releases; plan the test without one.

## 3. Release exactly one setting

1. Plant page → card of the **target inverter** → “Writing”.
2. Tick the master switch for this inverter, tick **only the one chosen setting**, save. The other
   inverters stay at the default (off). The card now shows “1 setting released”.
3. Wait until the inverter is `Online` again (saving rebuilds the polling generation).

## 4. The write

1. Inverter page of the target → “Change settings”. The dialog reads the value freshly. Compare it
   with your baseline and the display. If it differs, stop and find out why.
2. Enter the new value (keep it small and reversible) and open the preview. Check: target inverter
   (serial number and role), old → new, raw word and mask, the values of the other inverters
   before the change. The preview sends nothing. If the app refuses (model unknown, GEN port not
   Smart Load, a peer unreachable, value outside the range, conflict), it explains why; fix the
   cause rather than trying around it.
3. Confirm within 30 seconds. The app writes once, waits about a second and reads back.
4. Read the result:
   - `Read-back confirmed`: the device acknowledged and the register holds exactly the requested
     word. Now check the **display** of the target (and the other inverters) and record what you
     see with “Checked at the device: correct / not correct”.
   - `Rejected by the device`: the device answered with an exception code; nothing changed.
   - `Rejected`: nothing was sent (the reason is shown).
   - `Outcome unknown`: the answer is missing or malformed, or the read-back differs. **Do not
     repeat the write.** Look at the display, then at the current value in the dialog. A matching
     value later does not prove that the command set it.
5. Look at the other inverters in the result table: each is shown with its value before and after,
   and “same / different / unavailable” relative to the requested value. Record this in your
   notes; it is the observation that decides whether a later parallel use is safe for you.

## 5. Restore (a new operation)

Restoring the old value is a **new** operation with its own preview and confirmation; the app
never rolls back by itself. Do it after you have looked at the effect, then verify again at the
display. Release the setting again (plant page, uncheck, save) when the test is finished.

## 6. If something looks wrong

- Stop. Do not repeat writes. Remove the release (plant page) so nothing more can be written.
- Read the value at the display and, if needed, set it back with the inverter's own menu; this is
  independent of the app.
- Keep the audit log `/data/write-audit.jsonl` (every intent and result, with raw words and the
  observation of the peers) and the app log for the report. Do not include credentials.
- Report: version, inverter (serial, role), setting, old/new word, status, what the display showed,
  what the other inverters did.

## 7. What counts as a successful first test

The setting was written on one slave only, read back exactly, the display agrees, the other
inverters kept their values (or you know and accept what they did), the restore worked as a new
operation, and no other value on any inverter changed. Only then consider the next setting, and
only the same way: one inverter, one setting, preview, confirmation, display check.
