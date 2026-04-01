# XRPL Transaction Sequencing — Reference

## Sequence numbers
- Each account has `Sequence` starting at 1, incremented per applied transaction
- `terPRE_SEQ` — future sequence (retriable), `tefPAST_SEQ` — already used (permanent)
- `tec` failures still consume sequence number and fee

## Transaction queue (TxQ)
- Max 10 queued per account
- Fee escalation: `requiredFeeLevel = multiplier × (txCount² / txnsExpected²)`
- `terQUEUED` — accepted into queue, not yet applied

## Tickets
- `TicketCreate(count=N)` advances sequence by N+1, creates N ticket SLEs
- Ticket-based txns sort after sequence-based in canonical ordering
- Max 250 outstanding tickets per account
- Workload removes tickets optimistically from `acc.tickets` on use

## Error codes
| Code | Type | Meaning |
|------|------|---------|
| `terPRE_SEQ` | retriable | Sequence in the future |
| `tefPAST_SEQ` | permanent | Sequence already used |
| `terQUEUED` | retriable | In queue, waiting |
| `tefMAX_LEDGER` | permanent | LastLedgerSequence passed |
| `tecDIR_FULL` | fee claimed | Too many tickets (>250) |
