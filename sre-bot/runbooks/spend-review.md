---
title: Weekly spend & commitment review
when_to_use: The weekly review trigger fires.
prerequisites:
- the aws-cost verbs in your skill.verbs
steps:
- 'Spend: last 7 and 30 days by service, and the month''s forecast; note moves over 20%.'
- 'Commitments: reservation and savings-plan utilization and coverage.'
- 'Recommendations: check the usage behind them is steady and not about to change.'
- Propose a purchase only with the offering, total commitment, monthly saving and break-even.
- Report spend, notable changes, commitment health and any proposal; log the week's numbers.
verification: The report is in the case and the week's numbers are in the log.
rollback: A purchase cannot be rolled back once approved — which is why it is always operator-approved and capped.
owner: the bot's operator
---
# Weekly spend & commitment review

Fires weekly. The goal is a short report, and a purchase proposal only when one
clearly pays for itself.

1. **Spend.** `aws.cost_and_usage` for the last 7 and 30 days grouped by
   SERVICE; `aws.cost_forecast` for the month. Note anything that moved more
   than 20% week over week, with the likely cause.
2. **Commitments.** `aws.reservation_utilization` / `aws.reservation_coverage`,
   `aws.savings_plans_utilization` / `aws.savings_plans_coverage`. Low
   utilization means over-commitment; low coverage on steady usage means
   under-commitment.
3. **Recommendations.** `aws.savings_plans_recommendation` and
   `aws.reservation_recommendation` (30-day lookback). Treat them as input,
   not instructions: check that the usage behind them is steady (not a one-off
   spike) and not about to change (a planned migration, a decommission —
   `knowledge/` and open cases).
4. **Propose a purchase** only with: the offering
   (`aws.savings_plans_offerings` / `aws.reserved_instance_offerings`), the
   hourly commitment, the **total** commitment (what the spend cap governs —
   the purchase verb verifies it against the offering), the expected monthly
   saving, and the break-even. Purchases are always operator-approved and
   capped; a purchase over the cap is refused, so split nothing to get under it.
5. Report: spend, notable changes, commitment health, any proposal. Log the
   week's numbers on this case (`conductor bot episode`).
