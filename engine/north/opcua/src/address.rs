//! The address space of one device (Python `_add_device`): channel and device folders, one folder
//! per tag group, a variable per enabled tag. Pure: the server turns the plan into nodes.

use std::collections::HashSet;

use opcua::types::DataTypeId;

use opencap_core::config::{Device, Tag};

use crate::values::data_type_id;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FolderPlan {
    /// the NodeId string, `<channel>.<device>.<group path>`
    pub path: String,
    pub name: String,
    pub parent: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct VarPlan {
    pub tag: String,
    /// the NodeId string
    pub path: String,
    pub name: String,
    pub parent: String,
    pub data_type: DataTypeId,
    pub description: Option<String>,
    /// scaled tags are AnalogItemType in Python; kept as a flag for the server
    pub analog: bool,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct DevicePlan {
    /// `<channel>.<device name>`: the device folder's NodeId string
    pub base: String,
    pub folders: Vec<FolderPlan>,
    pub vars: Vec<VarPlan>,
    /// tag name -> why it has no node
    pub skipped: Vec<(String, String)>,
}

/// The NodeId string of a device's folder.
pub fn device_base(dev: &Device) -> String {
    format!("{}.{}", dev.channel, dev.name)
}

/// The NodeId string of a tag, e.g. `SITE.PLC.LTI002.Pv`.
pub fn tag_path(dev: &Device, tag: &str) -> String {
    format!("{}.{tag}", device_base(dev))
}

pub fn plan_device(dev: &Device) -> DevicePlan {
    let base = device_base(dev);
    let mut plan = DevicePlan { base: base.clone(), ..Default::default() };
    let mut folders: HashSet<String> = HashSet::from([base.clone()]);
    let mut variables: HashSet<String> = HashSet::new();
    let mut tags: Vec<&Tag> = dev.tags.iter().filter(|t| t.enabled).collect();
    tags.sort_by_key(|t| t.name.matches('.').count()); // stable: fewest groups first
    for tag in tags {
        let parts: Vec<&str> = tag.name.split('.').collect();
        let groups: Vec<String> = (0..parts.len() - 1).map(|k| format!("{base}.{}", parts[..=k].join("."))).collect();
        let path = format!("{base}.{}", tag.name);
        let clash = groups
            .iter()
            .find(|g| variables.contains(*g))
            .cloned()
            .or_else(|| folders.contains(&path).then(|| path.clone()));
        if let Some(c) = clash {
            plan.skipped.push((tag.name.clone(), format!("{c} is both a tag and a tag group")));
            continue;
        }
        let mut parent = base.clone();
        for (part, group) in parts[..parts.len() - 1].iter().zip(&groups) {
            if folders.insert(group.clone()) {
                plan.folders.push(FolderPlan {
                    path: group.clone(),
                    name: (*part).to_string(),
                    parent: parent.clone(),
                });
            }
            parent = group.clone();
        }
        variables.insert(path.clone());
        plan.vars.push(VarPlan {
            tag: tag.name.clone(),
            path,
            name: parts[parts.len() - 1].to_string(),
            parent,
            data_type: data_type_id(tag),
            description: tag.description.clone().filter(|d| !d.is_empty()),
            analog: tag.scale.is_some(),
        });
    }
    plan
}
