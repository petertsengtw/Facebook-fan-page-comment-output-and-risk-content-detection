# 分類提示詞（規格 §8.3）

檔名即版本：`classify_v1.txt`、`classify_v2.txt`…，版本號寫入 `classifications.model_version`
以便日後比對重跑結果。

每次修改提示詞後**必須**執行 `smc eval` 並將結果記入 `docs/eval-log.md`（規格 §8.4），
避免無據調參。

> `classify_v1.txt` 於 M5 建立。
