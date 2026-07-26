# Scientific endpoint normalization and label policy

PRING-PACKAGE preserves the submitted endpoint record and adds a deterministic,
auditable interpretation layer. The implementation is deliberately
conservative: sharing a concentration unit does not make two pharmacological
endpoints biologically interchangeable.

## Endpoint meanings

| Endpoint | Scientific meaning | What a smaller concentration indicates | Required interpretation |
|---|---|---|---|
| IC50 | Assay-dependent concentration producing 50% inhibition. Absolute and relative IC50 definitions can differ. | Greater inhibitory potency in that assay. | Retain assay design, substrate/agonist concentration, incubation, curve model, and qualifier. Do not call IC50 an affinity constant. |
| Ki | Equilibrium inhibition constant estimated under a specified inhibition/binding model. | Stronger inhibition-associated affinity under that model. | Retain the model and experimental conditions. Do not derive Ki from IC50 unless the applicable assumptions and required concentrations are known. |
| Kd | Equilibrium dissociation constant measured directly in a binding experiment. | Higher binding affinity at equilibrium. | Treat as binding affinity, not functional efficacy or CYP inhibition potency. |
| EC50 | Concentration producing half of the measured maximal effect of the compound. | Greater functional potency in that assay system. | Retain efficacy, system, response direction, and curve definition. EC50 is not direct binding affinity. |
| AC50 | Concentration producing 50% of the fitted assay activity range, commonly used in high-throughput screening. | Greater assay activity potency. | Retain assay endpoint, curve-fit/hit-call information, efficacy, and quality flags. It is not automatically equivalent to EC50 or IC50. |

The definitions follow the
[NCATS Assay Guidance Manual](https://www.ncbi.nlm.nih.gov/books/NBK91994/),
[IUPHAR/BPS Guide to Pharmacology terminology](https://www.guidetopharmacology.org/helpPage.jsp),
and the [US EPA ToxCast Owner's
Manual](https://www.epa.gov/sites/default/files/2018-04/documents/toxcastownermanual4252018.pdf).

## Unit and scale normalization

For a positive concentration `x` reported in unit `u`, PRING calculates:

```text
C_M = x × unit_to_molar(u)
pX  = -log10(C_M)
```

Supported concentration units are M, mM, µM/uM, nM, and pM, including their
Unit Ontology identifiers. A pIC50, pKi, pKd, pEC50, or pAC50 record is
dimensionless and is converted back with `C_M = 10^(-pX)`. Supplying both a pX
value and a concentration unit is treated as a scale conflict.

Mass concentrations such as mg/L cannot be converted without a compound
molecular mass and an explicitly registered identity. PRING therefore marks
them `unsupported_unit` instead of guessing. Zero, negative, non-finite, or
unitless concentration values are ineligible for threshold labels.

IC50 is never automatically converted to Ki. The Cheng–Prusoff relationship is
valid only under defined competitive-mechanism assumptions and requires
assay-specific quantities such as substrate/ligand concentration and Km/Kd.
See [Cheng and Prusoff
(1973)](https://pubmed.ncbi.nlm.nih.gov/4202581/).

## Qualifiers and intervals

Qualifiers are normalized to `eq`, `lt`, `le`, `gt`, `ge`, `approx`, `range`,
or `unknown`. They produce explicit lower and/or upper molar bounds. Given
activity threshold `τ`:

- an exact value or complete interval with upper bound `≤ τ` supports active;
- a complete interval strictly above `τ` supports weak/negative only when
  `--weak-activity-as-negative true`;
- a bound or range crossing `τ` is ambiguous;
- `> τ` supports weak/negative, while `≥ τ` includes the active boundary and
  therefore abstains when its stored bound equals `τ`;
- approximate values without a quantified uncertainty interval abstain; and
- pX inequalities reverse direction when transformed to molar space.

This policy follows the Assay Guidance Manual recommendation to report
out-of-range concentration-response estimates as bounds rather than treating
extrapolated values as exact.

## Versioned label policy

The policy identifier is:

```text
pring-endpoint-activity-v3-endpoint-aware
```

Numeric threshold labeling is restricted to IC50, Ki, Kd, EC50, and AC50 and
requires `--activity-threshold-um`. Km and generic `INH`, `Potency`, or
`Activity` values remain numerically available but cannot create a
threshold-derived label. A source-declared PubChem activity outcome can still
support a source-asserted label when no comparable numeric endpoint exists.

Each endpoint stores:

- the original and canonical endpoint names;
- endpoint family, quantity, and short semantic definition;
- raw value/unit/qualifier and normalized molar interval;
- pX scale and value where valid;
- normalization status, threshold eligibility, and exclusion reason;
- supervision label and policy identifier; and
- decision reason, evidence basis, and reliability category.

Reliability categories are descriptive provenance, not calibrated
probabilities:

| Category | Meaning |
|---|---|
| `source_asserted` | Active/inactive was supplied by the source; PRING has not independently reproduced the assay decision. |
| `threshold_supported` | The complete normalized interval supports the configured threshold decision. |
| `concordant_source_and_threshold` | Source outcome and numeric threshold decision agree. |
| `conflicting` | Source and numeric evidence disagree, or source outcomes conflict; PRING abstains. |
| `insufficient` / `policy_abstention` | Evidence cannot support the requested binary assignment. |

At compound–target level, positive-only evidence produces label 1 and
negative-only evidence produces label 0. A pair with both positive and negative
endpoint evidence is excluded from supervised tables and remains reviewable in
the graph. Unobserved pairs remain candidates, not negatives.

## Scientific use

A single 10 µM threshold across endpoint types is a declared harmonization
choice for the CYP binary-classification case study. It does not prove that
10 µM has the same mechanistic interpretation for IC50, Ki, Kd, EC50, and
AC50. Thesis-grade analysis should report endpoint counts and results by type,
repeat the analysis with endpoint-restricted datasets, test plausible
thresholds, and retain assay-level provenance.

Recommended acceptance checks:

1. every threshold-labeled numeric endpoint has a supported type, positive
   finite molar interval, known qualifier, policy ID, and decision reason;
2. no unitless or mass-concentration record is threshold labeled;
3. no numeric-only endpoint is labeled without a declared threshold;
4. bounds crossing the threshold and all source/numeric conflicts abstain;
5. IC50, Ki, Kd, EC50, and AC50 remain distinct in exports and reports;
6. train/validation/test splits are created after final pair labels and remain
   group-disjoint; and
7. sensitivity results are reported separately rather than used to tune on the
   final test set.
