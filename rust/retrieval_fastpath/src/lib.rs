//! Optional CPU post-processing for DRUID. Authorization remains in Python!
//! The extension never inspects, constructs, or changes access predicates.
use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

/// RRF over ranked ID lists. Return results in Python's insertion-stable
/// score order. Sorting a Vec with sort_by is stable on Rust's standard library.
#[pyfunction]
fn rrf(
    branches: Vec<(String, Vec<(String, i64)>)>,
    k: i64,
) -> PyResult<Vec<(String, f64, Vec<String>)>> {
    if k < 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "RRF k must be nonnegative",
        ));
    }
    let mut indices: HashMap<String, usize> = HashMap::new();
    let mut ordered: Vec<(String, f64, HashSet<String>)> = Vec::new();
    for (branch_name, hits) in branches {
        for (position, (chunk_id, rank)) in hits.into_iter().enumerate() {
            let effective_rank = if rank > 0 { rank } else { (position + 1) as i64 };
            let denominator = k.checked_add(effective_rank).ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("rank overflows i64")
            })?;
            if denominator <= 0 {
                return Err(pyo3::exceptions::PyValueError::new_err("invalid RRF rank"));
            }
            let contribution = 1.0 / denominator as f64;
            if let Some(&idx) = indices.get(&chunk_id) {
                let found = &mut ordered[idx];
                found.1 += contribution;
                found.2.insert(branch_name.clone());
            } else {
                let idx = ordered.len();
                indices.insert(chunk_id.clone(), idx);
                let mut contributors = HashSet::new();
                contributors.insert(branch_name.clone());
                ordered.push((chunk_id, contribution, contributors));
            }
        }
    }
    ordered.sort_by(|left, right| right.1.total_cmp(&left.1));
    Ok(ordered
        .into_iter()
        .map(|(id, score, branches)| {
            let mut names: Vec<String> = branches.into_iter().collect();
            names.sort();
            (id, score, names)
        })
        .collect())
}

/// Python str[:max_chars].rsplit(" ", 1)[0] semantics, on Unicode scalar
/// values. Negative budgets behave like Python's negative slice limit.
fn trim_passage_one(text: &str, max_chars: i64) -> String {
    let chars: Vec<char> = text.chars().collect();
    let size = chars.len() as i64;
    if size <= max_chars {
        return text.to_owned();
    }
    let end = if max_chars < 0 {
        (size + max_chars).max(0) as usize
    } else {
        (max_chars as usize).min(chars.len())
    };
    let prefix: String = chars[..end].iter().collect();
    match prefix.rfind(' ') {
        Some(idx) => prefix[..idx].to_owned(),
        None => prefix,
    }
}

/// Batch avoids crossing the Python/Rust boundary for every rerank candidate.
#[pyfunction]
fn truncate_passages(passages: Vec<String>, max_chars: i64) -> Vec<String> {
    passages
        .iter()
        .map(|s| trim_passage_one(s, max_chars))
        .collect()
}

#[pymodule]
fn druid_retrieval_fastpath(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rrf, m)?)?;
    m.add_function(wrap_pyfunction!(truncate_passages, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stable_ties_and_duplicate_rank() {
        let x = rrf(
            vec![
                ("dense".into(), vec![("a".into(), 1), ("b".into(), 0)]),
                ("bm25".into(), vec![("a".into(), 1)]),
            ],
            60,
        ).unwrap();
        assert_eq!(x[0].0, "a");
        assert_eq!(x[0].2, vec!["bm25", "dense"]);
        assert_eq!(x[1].0, "b");
    }
    #[test]
    fn utf8_truncation_parity() {
        assert_eq!(trim_passage_one("🚀 hello world", 9), "🚀 hello");
        assert_eq!(trim_passage_one("short", 20), "short");
    }
}
