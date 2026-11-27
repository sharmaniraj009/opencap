//! PKI folder upkeep. async-opcua writes every refused client certificate into `<pki_dir>/rejected/`
//! so an administrator can move it to `trusted/`. Anyone who can reach the port can present fresh
//! certificates, so the folder is capped: the oldest files go first (#248).

use std::path::Path;

/// Files kept in `rejected/`.
pub const MAX_REJECTED: usize = 100;

/// Delete the oldest files of `dir` until at most `keep` remain. Returns how many were removed.
/// A missing folder is fine (nothing refused yet).
pub fn prune_rejected(dir: &Path, keep: usize) -> usize {
    let Ok(rd) = std::fs::read_dir(dir) else { return 0 };
    let mut files: Vec<(std::time::SystemTime, std::path::PathBuf)> = rd
        .flatten()
        .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
        .map(|e| (e.metadata().and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH), e.path()))
        .collect();
    if files.len() <= keep {
        return 0;
    }
    files.sort(); // oldest first
    let excess = files.len() - keep;
    files.into_iter().take(excess).filter(|(_, p)| std::fs::remove_file(p).is_ok()).count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_oldest_files_go_first_and_a_missing_folder_is_fine() {
        let dir = std::env::temp_dir().join(format!("opencap-rejected-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(prune_rejected(&dir, 3), 0);
        std::fs::create_dir_all(&dir).unwrap();
        for i in 0..6 {
            let p = dir.join(format!("c{i}.der"));
            std::fs::write(&p, b"x").unwrap();
            let t = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000 + i);
            std::fs::File::options().write(true).open(&p).unwrap().set_modified(t).unwrap();
        }
        assert_eq!(prune_rejected(&dir, 3), 3);
        let mut left: Vec<String> =
            std::fs::read_dir(&dir).unwrap().flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        assert_eq!(left, ["c3.der", "c4.der", "c5.der"]);
        assert_eq!(prune_rejected(&dir, 3), 0);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
