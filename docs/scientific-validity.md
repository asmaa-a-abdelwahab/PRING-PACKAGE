# Scientific validity

PRING-PACKAGE makes data lineage and leakage controls enforceable, but it cannot
make a modeling claim valid by itself.

## Minimum defensible protocol

1. Define the prediction unit, target population, candidate space, and label
   policy before fitting models.
2. Freeze a compound-, scaffold-, similarity-component-, or time-aware outer
   split appropriate to the claim.
3. Fit all preprocessing and learned graph transformations on training data.
4. Generate stacking inputs out of fold for training rows.
5. Select hyperparameters, seeds, calibration, and thresholds without test
   outcomes.
6. Evaluate the locked pipeline on the outer test once.
7. Audit overlap with external validation data and the graph evidence used to
   construct features.

## Common risks

- Bioassay labels can mix protocols, concentrations, endpoints, and reporting
  conventions.
- Missing negatives are not necessarily biological inactivity.
- Similar compounds and duplicated evidence can cross naive random splits.
- Knowledge-graph topology can reveal held-out labels through direct or
  evidence-path edges.
- Text-mined assertions and model predictions must not be recycled as ground
  truth.
- Per-target performance can differ sharply from aggregate metrics.

## Endpoint-normalization validity

IC50, Ki, Kd, EC50, and AC50 are normalized to molar concentration and may be
represented as `pX = -log10(X[M])`, but they are not interchangeable response
variables. IC50 is assay-dependent inhibition potency; Ki is an inhibition
constant estimated under a stated model; Kd is an equilibrium binding
dissociation constant; EC50 is functional potency; and AC50 is a
screening/curve-fit activity concentration. PRING does not automatically
convert between them.

The v3 label policy requires a predeclared positive concentration threshold for
numeric evidence, preserves inequality bounds, abstains for intervals crossing
the threshold, and excludes unsupported units, nonpositive values, approximate
values without an uncertainty interval, Km, and generic Activity/Potency/INH
measurements from numeric threshold labeling. A source-declared PubChem
active/inactive outcome remains traceable as `source_asserted`; it is not
represented as independently verified biological truth.

See [Scientific endpoint normalization and label
policy](ENDPOINT_NORMALIZATION.md) for endpoint definitions, formulas,
decision rules, and acceptance tests.

## Reporting

Report class prevalence, split construction, sample exclusions, calibration,
confidence intervals, per-target results, abstentions, failed inputs, and
external-validity limitations. Qualify predictions as computational hypotheses,
not clinical or causal conclusions.
