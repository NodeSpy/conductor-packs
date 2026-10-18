---
title: Environment variable change requests
when_to_use: Someone asks to change an environment variable (a feature flag, a timeout, a connection string).
prerequisites:
- the case shows the requester
steps:
- 'Check who asked: anyone but your operator runs on their trust — investigate and propose.'
- Find where it lives (task definition, manifest, parameter store); never read or echo a secret.
- 'Check the effect: what reads it, whether a restart is needed, what breaks if wrong.'
- Propose the exact change (old → new, why, who asked, rollout and rollback).
- After it is applied, confirm the service picked it up and close the case.
verification: The service runs with the new value and its health is unchanged.
rollback: Propose the old value back, through the same verb or Terraform change.
owner: the bot's operator
---
# Environment variable change requests

Someone asks to change an environment variable (a feature flag, a timeout, a
connection string).

1. **Who asked?** The case shows the requester. A request from anyone but your
   operator runs on that person's trust: you investigate and propose; your
   operator approves.
2. **Find where it lives.** The service's task definition, deployment
   manifest, parameter store or secrets manager — `knowledge/` should say.
   Never read or echo a secret's value; a secret change is a request for your
   operator to make.
3. **Check the effect.** What reads it, what the new value does, whether a
   restart or redeploy is needed, and what breaks if it is wrong.
4. **Propose the exact change** through the verb that makes it (or a
   Terraform change, if the variable is managed in Terraform — see
   terraform-change.md), with: old → new, why, who asked, rollout and rollback.
5. After it is applied, confirm the service picked it up and close the case.
