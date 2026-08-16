import pandas as pd
from statsmodels.genmod.bayes_mixed_glm import PoissonBayesMixedGLM

df = pd.read_csv("/home/david/birdnet/mlops/scripts/extracted_data.csv")
for col in ["tempf", "humidity", "baromrelin", "time_of_day"]:
    df[col] = (df[col] - df[col].mean()) / df[col].std()

model = PoissonBayesMixedGLM.from_formula(
    "count ~ tempf + humidity + baromrelin + time_of_day",
    {"species": "0 + C(Com_Name)"},
    data=df,
)
result = model.fit_vb()
print([a for a in dir(result) if not a.startswith("_")])
