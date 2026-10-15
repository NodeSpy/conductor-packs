# Terraform: plan → approve → apply

Infrastructure changes are always plan first, apply a SAVED plan second, and the
apply is always approved by an operator.

1. Work in your own working directory. Check out the infrastructure repo there
   (or use the Terraform connector's working directory if your operator set
   one up).
2. `terraform init`, then `terraform plan -out=plan.tfplan` (or the
   `terraform.plan` verb with `out: plan.tfplan`). Planning is read-only.
3. **Read the plan.** `terraform show -no-color plan.tfplan`. Every resource
   that is replaced or destroyed must be expected; if anything surprises you,
   stop and report instead of proposing.
4. Propose the apply of exactly that file, FROM THE DIRECTORY IT RUNS IN (the
   root module — e.g. `envs/prod`):

       cd envs/prod
       conductor member propose-command --file plan.tfplan -- terraform apply plan.tfplan

   The directory is part of what is approved: the same apply from another
   directory (another root module, another state) is a different action and
   will be refused. Conductor records the SHA-256 of `envs/prod/plan.tfplan`
   as the broker will read it. In your
   message to the operator: the summary line (`N to add, M to change, K to
   destroy`), why, the risk, and how to roll back.
5. `conductor member wait <proposal>`. If approved, run exactly
   `terraform apply plan.tfplan` from that same directory — the approval is a
   single-use grant for that command, that directory and those bytes; any other
   command, directory, or a changed plan file, is refused.
6. Verify the change took effect, record the outcome in the case, and write
   anything learned to `knowledge/`.
