"""Economics and evidence lane: arguments the arena can verify, built from the case file and the data.

compile_case(case, standard)  -> Economics      (hollerau.contracts.Economics)
compile_evidence(case, data) -> list[Claim]   (hollerau.contracts.Claim)
signals(case, data)          -> dict           (for the planner, never sent to a supplier)
"""
