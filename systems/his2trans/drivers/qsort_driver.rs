// qsort: the translation's API is quickSort(arr: &mut [i32], low: usize, high: usize) and
// partition(arr: &mut [i32], low: usize, high: usize) -> usize. The generator only emits
// 0 <= low, high < n (in range for both signatures).
fn dispatch(tok: &[&str]) {
    let op = tok[0];
    let n = rg_int(tok[1]) as usize;
    let mut a: Vec<i32> = (0..n).map(|i| rg_int(tok[2 + i]) as i32).collect();
    let low = rg_int(tok[2 + n]) as usize;
    let high = rg_int(tok[3 + n]) as usize;
    if op == "qs" {
        crate::src_qsort::quickSort(&mut a[..], low, high);
    } else if op == "part" {
        let r = crate::src_qsort::partition(&mut a[..], low, high);
        pi(r as i64);
    }
    for v in &a {
        pi(*v as i64);
    }
}
