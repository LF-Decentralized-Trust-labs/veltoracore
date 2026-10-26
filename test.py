import pandas as pd
from config import OUTPUT_DIR  # PATH-1
tol = pd.read_csv(f"{OUTPUT_DIR}/tolerance_bins.csv")
print(tol.columns)
print(tol.head())

