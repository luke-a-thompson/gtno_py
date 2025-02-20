Chemical Graph Transformer Neural Operator (CGTNO) is a neural operator for molecular dynamics trained on the MD17 dataset.

Please install with `poetry install --with dev` if you want type checking (i.e., for mypy).

TODO:
1. Review `model.py`
1. Training loop / datasets
1. Add unit tests
1. Architectural improvements as per comments

# Data sources
The same as used in EGNO: https://www.sgdml.org/

Relevant papers:
1. EGNO: https://arxiv.org/pdf/2401.11037
    * General graph neural operator architecture
1. https://arxiv.org/pdf/2203.06442
    * Dataset setup in more detail
1. http://www.sgdml.org/
    * Dataset source
1. https://arxiv.org/pdf/2302.14376
    * Heterogenous attention - We specify it for graphs
1. https://arxiv.org/pdf/2108.08481
    * Neural operator theory