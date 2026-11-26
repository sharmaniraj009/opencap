//! Tag list upload: the Symbol Object instance list (controller scope and every program scope)
//! and the templates of the structures it uses. Read services only.

use std::collections::HashMap;

use crate::cip::{self, CLASS_SYMBOL, CLASS_TEMPLATE, STATUS_OK, STATUS_PARTIAL};
use crate::conn::{Conn, NetError};
use crate::tags::{
    is_program_scope, is_user_tag, parse_symbols, parse_template, parse_template_attributes, struct_ids_of, tag_info,
    Kind, TagInfo, TagList, Template, ATTR_EXTERNAL_ACCESS, SYMBOL_ATTRIBUTES, TEMPLATE_ATTRIBUTES,
};

fn proto(e: String) -> NetError {
    NetError::Protocol(e)
}

/// Failure of an upload: transport, or a status the controller answered with.
#[derive(Debug)]
pub enum UploadError {
    Net(NetError),
    Refused(String),
}

impl From<NetError> for UploadError {
    fn from(e: NetError) -> Self {
        UploadError::Net(e)
    }
}

impl Conn {
    /// Upload the tag list of the controller (`revision_major` decides whether External Access
    /// is asked for) and the templates it needs.
    pub async fn upload_tag_list(&mut self, revision_major: u8, programs: bool) -> Result<TagList, UploadError> {
        let with_access = revision_major >= 18;
        let mut attrs = SYMBOL_ATTRIBUTES.to_vec();
        if with_access {
            attrs.push(ATTR_EXTERNAL_ACCESS);
        }
        let mut tags: HashMap<String, TagInfo> = HashMap::new();
        let mut scopes: Vec<String> = Vec::new();
        for sym in self.symbols(&[], &attrs, with_access).await? {
            if is_program_scope(&sym) {
                scopes.push(sym.name.clone());
            } else if is_user_tag(&sym) {
                tags.insert(sym.name.clone(), tag_info(&sym));
            }
        }
        if programs {
            for scope in scopes {
                let mut prefix = Vec::new();
                prefix.push(0x91);
                prefix.push(scope.len() as u8);
                prefix.extend_from_slice(scope.as_bytes());
                if scope.len() % 2 == 1 {
                    prefix.push(0);
                }
                for sym in self.symbols(&prefix, &attrs, with_access).await? {
                    if is_user_tag(&sym) {
                        tags.insert(format!("{scope}.{}", sym.name), tag_info(&sym));
                    }
                }
            }
        }
        let mut templates: HashMap<u16, Template> = HashMap::new();
        let mut want: Vec<u16> =
            tags.values().filter_map(|t| if let Kind::Struct(id) = t.node.kind { Some(id) } else { None }).collect();
        while let Some(id) = want.pop() {
            if templates.contains_key(&id) {
                continue;
            }
            let t = self.template(id).await?;
            want.extend(struct_ids_of(&t.members));
            templates.insert(id, t);
        }
        Ok(TagList::new(tags, templates))
    }

    /// Every symbol of one scope (`prefix` is the program symbol segment, or empty).
    async fn symbols(
        &mut self,
        prefix: &[u8],
        attrs: &[u16],
        with_access: bool,
    ) -> Result<Vec<crate::tags::Symbol>, UploadError> {
        let mut out = Vec::new();
        let mut instance = 0u32;
        loop {
            let mut path = prefix.to_vec();
            path.extend(cip::class_instance_path(CLASS_SYMBOL, instance));
            let req = cip::get_instance_attribute_list_request(&path, attrs).map_err(proto)?;
            let reply = self.send_connected(&req).await?;
            if reply.status != STATUS_OK && reply.status != STATUS_PARTIAL {
                return Err(UploadError::Refused(format!("tag list: {}", cip::status_description(reply.status))));
            }
            let batch = parse_symbols(&reply.data, with_access).map_err(proto)?;
            let last = batch.last().map(|s| s.instance);
            out.extend(batch);
            match (reply.status, last) {
                (STATUS_PARTIAL, Some(last)) => instance = last + 1,
                (STATUS_PARTIAL, None) => return Err(UploadError::Refused("tag list: empty partial reply".into())),
                _ => return Ok(out),
            }
        }
    }

    async fn template(&mut self, id: u16) -> Result<Template, UploadError> {
        let path = cip::class_instance_path(CLASS_TEMPLATE, id as u32);
        let req = cip::get_attribute_list_request(&path, &TEMPLATE_ATTRIBUTES).map_err(proto)?;
        let reply = self.send_connected(&req).await?;
        if reply.status != STATUS_OK {
            return Err(UploadError::Refused(format!("template {id:#06x}: {}", cip::status_description(reply.status))));
        }
        let (words, size, count, handle) = parse_template_attributes(&reply.data).map_err(proto)?;
        let total = (words as usize * 4).saturating_sub(23);
        let mut def: Vec<u8> = Vec::new();
        for _ in 0..1000 {
            let want = (total.saturating_sub(def.len())).min(u16::MAX as usize) as u16;
            let req = cip::read_template_request(id, def.len() as u32, want).map_err(proto)?;
            let r = self.send_connected(&req).await?;
            if r.status != STATUS_OK && r.status != STATUS_PARTIAL {
                return Err(UploadError::Refused(format!("template {id:#06x}: {}", cip::status_description(r.status))));
            }
            def.extend_from_slice(&r.data);
            if r.status == STATUS_OK || r.data.is_empty() {
                break;
            }
        }
        parse_template(&def, count, handle, size).map_err(|e| UploadError::Refused(format!("template {id:#06x}: {e}")))
    }
}
