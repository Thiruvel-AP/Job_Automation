"""Weighted ATS score for the Anaplan Data Scientist JD.
score = sum(w * hit) / sum(w). Strict = literal match in the CV text.
Claimable = terms with evidence in the fact bank / his own CV (honest ceiling)."""
import re, sys
T = [  # (label, weight, regex, claimable?)
 ("Python",3,r"\bpython\b",1), ("SQL",3,r"\bsql\b",1),
 ("machine learning",3,r"machine learning",1), ("deep learning",3,r"deep learning",1),
 ("time series forecasting",3,r"time.?series|forecast",1), ("XGBoost",3,r"xgboost",1),
 ("LightGBM",3,r"lightgbm",0), ("PyTorch",3,r"pytorch",1), ("pandas",3,r"\bpandas\b",1),
 ("feature engineering",3,r"feature engineering",1), ("prototype to production",3,r"prototype.{0,40}production",1),
 ("MLflow",3,r"mlflow",1),
 ("testing",2,r"\btest|pytest",1), ("CI/CD",2,r"ci/cd|\bci\b|github actions",1),
 ("code review",2,r"code review|reviewed pull requests",1), ("dbt",2,r"\bdbt\b",0), ("Snowflake",2,r"snowflake",0),
 ("Databricks",2,r"databricks",1), ("data pipelines",2,r"pipeline",1),
 ("cloud (AWS/GCP/Azure)",2,r"\baws\b|\bgcp\b|azure",1), ("Parquet",2,r"parquet",1),
 ("Optuna",2,r"optuna",0), ("Prefect/Airflow",2,r"prefect|airflow",0), ("MLOps",2,r"mlops",1),
 ("model monitoring",2,r"monitor",0), ("customer/client partnering",2,r"client|customer",1),
 ("demand forecasting/sensing",2,r"demand",0), ("cold start",2,r"cold.?start",0),
 ("hyperparameter tuning",2,r"hyperparameter",1), ("data warehouse",2,r"warehouse",0),
 ("supply chain",1,r"supply chain",0), ("retail",1,r"retail",0), ("enterprise AI",1,r"enterprise",0),
 ("MSc Data Science",1,r"msc data science",1), ("transactional data",1,r"transaction",1),
 ("temporal signals",1,r"temporal|date encoding|seasonal",1), ("scalable",1,r"scal",1),
 ("statistics",1,r"statistic",1),
]
def score(text):
    t = re.sub(r"\s+"," ",text.lower()); tot=sum(w for _,w,_,_ in T)
    hit=[(l,w) for l,w,rx,_ in T if re.search(rx,t)]
    return 100*sum(w for _,w in hit)/tot, {l for l,_ in hit}
tot=sum(w for _,w,_,_ in T); ceil=100*sum(w for _,w,_,c in T if c)/tot
if __name__=="__main__":
    for f in sys.argv[1:]:
        s,h=score(open(f).read())
        miss=[l for l,w,_,c in T if l not in h]
        print(f"{f}: strict={s:.1f}  claimable-ceiling={ceil:.1f}  total_weight={tot}")
        print("  missing:",", ".join(f"{l}{'' if c else '*'}" for l,w,_,c in T if l not in h))
    print("  (* = not claimable from the fact bank)")
