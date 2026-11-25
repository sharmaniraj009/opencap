/*
 * open62541 scenario client for the L1 SCADA suite (tests/scada/README.md). Same argv and JSON
 * protocol as tests/scada/asyncua_client.py (the reference). Prints JSON lines on stdout; the
 * last one is {"result": {...}}. Test-only; open62541 is MPL-2.0 (docs/architecture/oss-reuse.md).
 */
#include <open62541/client.h>
#include <open62541/client_config_default.h>
#include <open62541/client_highlevel.h>
#include <open62541/client_subscriptions.h>
#include <open62541/plugin/log_stdout.h>

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* --- arguments ------------------------------------------------------------------------------ */

#define MAX_ARGS 64
static const char *arg_keys[MAX_ARGS];
static const char *arg_vals[MAX_ARGS];
static int arg_count = 0;

static const char *arg(const char *name, const char *dflt) {
    for(int i = 0; i < arg_count; i++)
        if(strcmp(arg_keys[i], name) == 0)
            return arg_vals[i];
    if(!dflt) {
        fprintf(stderr, "--%s is required\n", name);
        exit(2);
    }
    return dflt;
}

static double num(const char *name, double dflt) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%g", dflt);
    return atof(arg(name, buf));
}

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

typedef struct { char **items; size_t n; } Lines;

static Lines read_lines(const char *path) {
    Lines l = {NULL, 0};
    FILE *f = fopen(path, "r");
    if(!f) { fprintf(stderr, "cannot open %s\n", path); exit(2); }
    char buf[4096];
    while(fgets(buf, sizeof(buf), f)) {
        size_t len = strcspn(buf, "\r\n");
        buf[len] = 0;
        if(len == 0) continue;
        l.items = realloc(l.items, (l.n + 1) * sizeof(char *));
        l.items[l.n++] = strdup(buf);
    }
    fclose(f);
    return l;
}

static UA_ByteString load_file(const char *path) {
    UA_ByteString out = UA_BYTESTRING_NULL;
    FILE *f = fopen(path, "rb");
    if(!f) return out;
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    out.data = (UA_Byte *)UA_malloc((size_t)size);
    out.length = fread(out.data, 1, (size_t)size, f);
    fclose(f);
    return out;
}

/* --- JSON output (into a growing buffer; one line per emit) --------------------------------- */

typedef struct { char *s; size_t len, cap; } Buf;

static void put(Buf *b, const char *fmt, ...) {
    va_list ap;
    for(;;) {
        va_start(ap, fmt);
        int n = vsnprintf(b->s ? b->s + b->len : NULL, b->s ? b->cap - b->len : 0, fmt, ap);
        va_end(ap);
        if(b->s && (size_t)n < b->cap - b->len) { b->len += (size_t)n; return; }
        b->cap = (b->cap + (size_t)n + 1) * 2;
        b->s = realloc(b->s, b->cap);
    }
}

static void put_escaped(Buf *b, const char *s, size_t len) {
    for(size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];
        if(c == '"' || c == '\\') put(b, "\\%c", c);
        else if(c == '\n') put(b, "\\n");
        else if(c < 0x20) put(b, "\\u%04x", c);
        else put(b, "%c", c);
    }
}

static void put_str(Buf *b, const char *s, size_t len) {
    put(b, "\"");
    put_escaped(b, s, len);
    put(b, "\"");
}

static void put_cstr(Buf *b, const char *s) { put_str(b, s, strlen(s)); }

static void put_uastr(Buf *b, const UA_String *s) { put_str(b, (const char *)s->data, s->length); }

static void put_status(Buf *b, UA_StatusCode code) { put(b, "\"0x%08X\"", code); }

static void put_nodeid(Buf *b, const UA_NodeId *id) {
    UA_String out = UA_STRING_NULL;
    UA_NodeId_print(id, &out);
    put_uastr(b, &out);
    UA_String_clear(&out);
}

static void put_scalar(Buf *b, const UA_DataType *t, const void *p) {
    switch(t->typeKind) {
    case UA_DATATYPEKIND_BOOLEAN: put(b, *(const UA_Boolean *)p ? "true" : "false"); break;
    case UA_DATATYPEKIND_SBYTE: put(b, "%d", *(const UA_SByte *)p); break;
    case UA_DATATYPEKIND_BYTE: put(b, "%u", *(const UA_Byte *)p); break;
    case UA_DATATYPEKIND_INT16: put(b, "%d", *(const UA_Int16 *)p); break;
    case UA_DATATYPEKIND_UINT16: put(b, "%u", *(const UA_UInt16 *)p); break;
    case UA_DATATYPEKIND_INT32: put(b, "%d", *(const UA_Int32 *)p); break;
    case UA_DATATYPEKIND_UINT32: put(b, "%u", *(const UA_UInt32 *)p); break;
    case UA_DATATYPEKIND_INT64: put(b, "%lld", (long long)*(const UA_Int64 *)p); break;
    case UA_DATATYPEKIND_UINT64: put(b, "%llu", (unsigned long long)*(const UA_UInt64 *)p); break;
    case UA_DATATYPEKIND_FLOAT: put(b, "%.9g", (double)*(const UA_Float *)p); break;
    case UA_DATATYPEKIND_DOUBLE: put(b, "%.17g", *(const UA_Double *)p); break;
    case UA_DATATYPEKIND_STRING: put_uastr(b, (const UA_String *)p); break;
    case UA_DATATYPEKIND_LOCALIZEDTEXT: put_uastr(b, &((const UA_LocalizedText *)p)->text); break;
    case UA_DATATYPEKIND_NODEID: put_nodeid(b, (const UA_NodeId *)p); break;
    case UA_DATATYPEKIND_DATETIME: {
        UA_DateTimeStruct d = UA_DateTime_toStruct(*(const UA_DateTime *)p);
        put(b, "\"%04u-%02u-%02uT%02u:%02u:%02u.%03uZ\"", d.year, d.month, d.day, d.hour, d.min, d.sec,
            d.milliSec);
        break;
    }
    default: put(b, "\"<%s>\"", t->typeName); break;
    }
}

static void put_variant(Buf *b, const UA_Variant *v) {
    if(!v || !v->type || !v->data) { put(b, "null"); return; }
    if(UA_Variant_isScalar(v)) { put_scalar(b, v->type, v->data); return; }
    put(b, "[");
    for(size_t i = 0; i < v->arrayLength; i++) {
        if(i) put(b, ",");
        put_scalar(b, v->type, (const char *)v->data + i * v->type->memSize);
    }
    put(b, "]");
}

static void emit(Buf *b) {
    fwrite(b->s, 1, b->len, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    b->len = 0;
}

static void emit_event(const char *name) {
    printf("{\"event\":\"%s\"}\n", name);
    fflush(stdout);
}

/* --- connections ---------------------------------------------------------------------------- */

static UA_ByteString g_cert, g_key;

static UA_MessageSecurityMode mode_of(const char *name) {
    if(strcmp(name, "Sign") == 0) return UA_MESSAGESECURITYMODE_SIGN;
    if(strcmp(name, "SignAndEncrypt") == 0) return UA_MESSAGESECURITYMODE_SIGNANDENCRYPT;
    return UA_MESSAGESECURITYMODE_NONE;
}

static void (*g_state_cb)(UA_Client *, UA_SecureChannelState, UA_SessionState, UA_StatusCode) = NULL;

/* Library log lines go to stderr (stdout carries the JSON protocol); warnings and up. */
static void log_stderr(void *ctx, UA_LogLevel level, UA_LogCategory category, const char *msg, va_list args) {
    (void)ctx; (void)category;
    if(level < UA_LOGLEVEL_WARNING) return;
    fprintf(stderr, "[open62541 %d] ", (int)level);
    vfprintf(stderr, msg, args);
    fputc('\n', stderr);
}
static UA_Logger g_logger = {log_stderr, NULL, NULL};

/* A client configured for (policyUri, mode); not yet connected. */
static UA_Client *new_client(const char *policyUri, UA_MessageSecurityMode mode) {
    UA_ClientConfig cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.logging = &g_logger;
    if(g_cert.length > 0)
        UA_ClientConfig_setDefaultEncryption(&cfg, g_cert, g_key, NULL, 0, NULL, 0);
    else
        UA_ClientConfig_setDefault(&cfg);
    UA_String_clear(&cfg.clientDescription.applicationUri);
    cfg.clientDescription.applicationUri = UA_STRING_ALLOC(arg("app-uri", "urn:opencap:scada-l1:client"));
    UA_LocalizedText_clear(&cfg.clientDescription.applicationName);
    cfg.clientDescription.applicationName = UA_LOCALIZEDTEXT_ALLOC("en", "opencap scada-l1 open62541 client");
    cfg.securityMode = mode;
    if(policyUri) cfg.securityPolicyUri = UA_STRING_ALLOC(policyUri);
    cfg.timeout = 10000;
    cfg.stateCallback = g_state_cb;
    return UA_Client_newWithConfig(&cfg);
}

static UA_StatusCode connect_with(UA_Client *client, const char *user, const char *password) {
    if(user) UA_ClientConfig_setAuthenticationUsername(UA_Client_getConfig(client), user, password);
    return UA_Client_connect(client, arg("endpoint", NULL));
}

static UA_Client *connect_default(void) {
    char uri[160];
    snprintf(uri, sizeof(uri), "http://opcfoundation.org/UA/SecurityPolicy#%s", arg("policy", "Basic256Sha256"));
    UA_MessageSecurityMode mode = mode_of(arg("mode", "SignAndEncrypt"));
    UA_Client *client = new_client(strcmp(arg("policy", "Basic256Sha256"), "None") == 0 ? NULL : uri, mode);
    UA_StatusCode rc = connect_with(client, arg("user", NULL), arg("password", ""));
    if(rc != UA_STATUSCODE_GOOD) {
        fprintf(stderr, "connect failed: %s\n", UA_StatusCode_name(rc));
        printf("{\"result\":{\"error\":\"connect 0x%08X %s\"}}\n", rc, UA_StatusCode_name(rc));
        exit(1);
    }
    return client;
}

static void finish(UA_Client *client) {
    UA_Client_disconnect(client);
    UA_Client_delete(client);
}

/* --- scenarios ------------------------------------------------------------------------------ */

static int s_endpoints(Buf *b) {
    UA_Client *client = new_client(NULL, UA_MESSAGESECURITYMODE_NONE);
    size_t n = 0;
    UA_EndpointDescription *eps = NULL;
    UA_StatusCode rc = UA_Client_getEndpoints(client, arg("endpoint", NULL), &n, &eps);
    if(rc != UA_STATUSCODE_GOOD) {
        put(b, "{\"result\":{\"error\":\"GetEndpoints 0x%08X\"}}", rc);
        UA_Client_delete(client);
        return 1;
    }
    put(b, "{\"result\":{\"endpoints\":[");
    for(size_t i = 0; i < n; i++) {
        UA_EndpointDescription *e = &eps[i];
        if(i) put(b, ",");
        put(b, "{\"url\":");
        put_uastr(b, &e->endpointUrl);
        put(b, ",\"policy\":");
        put_uastr(b, &e->securityPolicyUri);
        put(b, ",\"mode\":%d,\"tokens\":[", (int)e->securityMode);
        int seen[8] = {0}, first = 1;
        for(size_t t = 0; t < e->userIdentityTokensSize; t++) {
            int tt = (int)e->userIdentityTokens[t].tokenType;
            if(tt >= 0 && tt < 8 && !seen[tt]) seen[tt] = 1;
        }
        for(int tt = 0; tt < 8; tt++) {
            if(!seen[tt]) continue;
            put(b, first ? "%d" : ",%d", tt);
            first = 0;
        }
        put(b, "],\"server_cert\":%s}", e->serverCertificate.length > 0 ? "true" : "false");
    }
    put(b, "]}}");
    UA_Array_delete(eps, n, &UA_TYPES[UA_TYPES_ENDPOINTDESCRIPTION]);
    UA_Client_delete(client);
    return 0;
}

static int s_connect(Buf *b) {
    UA_Client *probe = new_client(NULL, UA_MESSAGESECURITYMODE_NONE);
    size_t n = 0;
    UA_EndpointDescription *eps = NULL;
    UA_StatusCode rc = UA_Client_getEndpoints(probe, arg("endpoint", NULL), &n, &eps);
    UA_Client_delete(probe);
    if(rc != UA_STATUSCODE_GOOD) {
        put(b, "{\"result\":{\"error\":\"GetEndpoints 0x%08X\"}}", rc);
        return 1;
    }
    char bad[256];
    snprintf(bad, sizeof(bad), "%s-wrong", arg("password", ""));
    const char *tokens[3][3] = {{"anonymous", NULL, NULL},
                                {"username", arg("user", NULL), arg("password", "")},
                                {"bad_password", arg("user", NULL), bad}};
    put(b, "{\"result\":{\"attempts\":[");
    int first = 1;
    for(size_t i = 0; i < n; i++) {
        char policy[256];
        snprintf(policy, sizeof(policy), "%.*s", (int)eps[i].securityPolicyUri.length,
                 eps[i].securityPolicyUri.data);
        for(int t = 0; t < 3; t++) {
            UA_Client *client = new_client(policy, eps[i].securityMode);
            UA_StatusCode crc = connect_with(client, tokens[t][1], tokens[t][2]);
            if(!first) put(b, ",");
            first = 0;
            put(b, "{\"policy\":");
            put_cstr(b, policy);
            put(b, ",\"mode\":%d,\"token\":\"%s\",", (int)eps[i].securityMode, tokens[t][0]);
            if(crc == UA_STATUSCODE_GOOD) {
                UA_Variant v;
                UA_Variant_init(&v);
                UA_StatusCode r = UA_Client_readValueAttribute(
                    client, UA_NODEID_NUMERIC(0, UA_NS0ID_SERVER_SERVERSTATUS_STATE), &v);
                if(r == UA_STATUSCODE_GOOD && v.type && UA_Variant_isScalar(&v))
                    put(b, "\"ok\":true,\"state\":%d,\"error\":null}", *(UA_Int32 *)v.data);
                else
                    put(b, "\"ok\":false,\"state\":null,\"error\":\"read 0x%08X\"}", r);
                UA_Variant_clear(&v);
                UA_Client_disconnect(client);
            } else {
                put(b, "\"ok\":false,\"state\":null,\"error\":\"0x%08X\"}", crc);
            }
            UA_Client_delete(client);
        }
    }
    put(b, "]}}");
    UA_Array_delete(eps, n, &UA_TYPES[UA_TYPES_ENDPOINTDESCRIPTION]);
    return 0;
}

static int s_browse(Buf *b) {
    UA_Client *client = connect_default();
    UA_UInt16 ns = (UA_UInt16)atoi(arg("ns", "2"));
    UA_UInt32 maxRefs = (UA_UInt32)atoi(arg("max-refs", "0"));
    size_t cap = 4096, head = 0, tail = 0, calls = 0;
    UA_NodeId *queue = (UA_NodeId *)malloc(cap * sizeof(UA_NodeId));
    queue[tail++] = UA_NODEID_NUMERIC(0, UA_NS0ID_OBJECTSFOLDER);
    put(b, "{\"result\":{\"nodes\":[");
    int first = 1;
    while(head < tail) {
        UA_NodeId parent = queue[head++];
        UA_BrowseRequest req;
        UA_BrowseRequest_init(&req);
        req.requestedMaxReferencesPerNode = maxRefs;
        UA_BrowseDescription bd;
        UA_BrowseDescription_init(&bd);
        bd.nodeId = parent;
        bd.browseDirection = UA_BROWSEDIRECTION_FORWARD;
        bd.referenceTypeId = UA_NODEID_NUMERIC(0, UA_NS0ID_HIERARCHICALREFERENCES);
        bd.includeSubtypes = true;
        bd.nodeClassMask = UA_NODECLASS_OBJECT | UA_NODECLASS_VARIABLE;
        bd.resultMask = UA_BROWSERESULTMASK_ALL;
        req.nodesToBrowse = &bd;
        req.nodesToBrowseSize = 1;
        UA_BrowseResponse resp = UA_Client_Service_browse(client, req);
        calls++;
        if(resp.responseHeader.serviceResult != UA_STATUSCODE_GOOD || resp.resultsSize != 1) {
            fprintf(stderr, "browse failed: 0x%08X\n", resp.responseHeader.serviceResult);
            UA_BrowseResponse_clear(&resp);
            continue;
        }
        /* Collect this node's references across BrowseNext calls. */
        UA_ReferenceDescription *refs = NULL;
        size_t nrefs = 0;
        UA_BrowseResult *res = &resp.results[0];
        UA_ByteString cp = UA_BYTESTRING_NULL;
        for(;;) {
            refs = realloc(refs, (nrefs + res->referencesSize + 1) * sizeof(UA_ReferenceDescription));
            for(size_t r = 0; r < res->referencesSize; r++)
                UA_ReferenceDescription_copy(&res->references[r], &refs[nrefs++]);
            UA_ByteString_clear(&cp);
            UA_ByteString_copy(&res->continuationPoint, &cp);
            if(cp.length == 0) break;
            UA_BrowseNextRequest nreq;
            UA_BrowseNextRequest_init(&nreq);
            nreq.continuationPoints = &cp;
            nreq.continuationPointsSize = 1;
            UA_BrowseNextResponse nresp = UA_Client_Service_browseNext(client, nreq);
            calls++;
            if(nresp.responseHeader.serviceResult != UA_STATUSCODE_GOOD || nresp.resultsSize != 1) {
                UA_BrowseNextResponse_clear(&nresp);
                break;
            }
            UA_BrowseResponse_clear(&resp);
            /* move the BrowseNext result into resp so the loop body reads it */
            UA_BrowseResponse_init(&resp);
            resp.results = nresp.results;
            resp.resultsSize = nresp.resultsSize;
            nresp.results = NULL;
            nresp.resultsSize = 0;
            UA_BrowseNextResponse_clear(&nresp);
            res = &resp.results[0];
        }
        UA_ByteString_clear(&cp);
        UA_BrowseResponse_clear(&resp);
        for(size_t r = 0; r < nrefs; r++) {
            UA_ReferenceDescription *ref = &refs[r];
            if(ref->nodeId.nodeId.namespaceIndex == ns) {
                if(!first) put(b, ",");
                first = 0;
                put(b, "{\"id\":");
                put_nodeid(b, &ref->nodeId.nodeId);
                put(b, ",\"name\":\"%u:", ref->browseName.namespaceIndex);
                put_escaped(b, (const char *)ref->browseName.name.data, ref->browseName.name.length);
                put(b, "\",\"class\":%d,\"parent\":", (int)ref->nodeClass);
                put_nodeid(b, &parent);
                put(b, "}");
                int known = 0;
                for(size_t q = 0; q < tail && !known; q++)
                    known = UA_NodeId_equal(&queue[q], &ref->nodeId.nodeId);
                if(!known) {
                    if(tail == cap) { cap *= 2; queue = realloc(queue, cap * sizeof(UA_NodeId)); }
                    UA_NodeId_copy(&ref->nodeId.nodeId, &queue[tail++]);
                }
            }
            UA_ReferenceDescription_clear(ref);
        }
        free(refs);
    }
    put(b, "],\"browse_calls\":%zu}}", calls);
    finish(client);
    return 0;
}

static int s_translate(Buf *b) {
    Lines paths = read_lines(arg("paths", NULL));
    UA_Client *client = connect_default();
    UA_UInt16 ns = (UA_UInt16)atoi(arg("ns", "2"));
    UA_BrowsePath *bps = (UA_BrowsePath *)UA_Array_new(paths.n, &UA_TYPES[UA_TYPES_BROWSEPATH]);
    for(size_t i = 0; i < paths.n; i++) {
        char *copy = strdup(paths.items[i]);
        size_t nseg = 1;
        for(char *c = copy; *c; c++) if(*c == '/') nseg++;
        bps[i].startingNode = UA_NODEID_NUMERIC(0, UA_NS0ID_OBJECTSFOLDER);
        bps[i].relativePath.elements =
            (UA_RelativePathElement *)UA_Array_new(nseg, &UA_TYPES[UA_TYPES_RELATIVEPATHELEMENT]);
        bps[i].relativePath.elementsSize = nseg;
        char *save = NULL, *seg = strtok_r(copy, "/", &save);
        for(size_t s = 0; seg && s < nseg; s++, seg = strtok_r(NULL, "/", &save)) {
            UA_RelativePathElement *el = &bps[i].relativePath.elements[s];
            el->referenceTypeId = UA_NODEID_NUMERIC(0, UA_NS0ID_HIERARCHICALREFERENCES);
            el->isInverse = false;
            el->includeSubtypes = true;
            el->targetName = UA_QUALIFIEDNAME_ALLOC(ns, seg);
        }
        free(copy);
    }
    UA_TranslateBrowsePathsToNodeIdsRequest req;
    UA_TranslateBrowsePathsToNodeIdsRequest_init(&req);
    req.browsePaths = bps;
    req.browsePathsSize = paths.n;
    UA_TranslateBrowsePathsToNodeIdsResponse resp = UA_Client_Service_translateBrowsePathsToNodeIds(client, req);
    if(resp.responseHeader.serviceResult != UA_STATUSCODE_GOOD || resp.resultsSize != paths.n) {
        put(b, "{\"result\":{\"error\":\"translate 0x%08X\"}}", resp.responseHeader.serviceResult);
    } else {
        put(b, "{\"result\":{\"results\":[");
        for(size_t i = 0; i < paths.n; i++) {
            if(i) put(b, ",");
            put(b, "{\"path\":");
            put_cstr(b, paths.items[i]);
            put(b, ",\"status\":");
            put_status(b, resp.results[i].statusCode);
            put(b, ",\"targets\":[");
            for(size_t t = 0; t < resp.results[i].targetsSize; t++) {
                if(t) put(b, ",");
                put_nodeid(b, &resp.results[i].targets[t].targetId.nodeId);
            }
            put(b, "]}");
        }
        put(b, "]}}");
    }
    UA_TranslateBrowsePathsToNodeIdsResponse_clear(&resp);
    UA_Array_delete(bps, paths.n, &UA_TYPES[UA_TYPES_BROWSEPATH]);
    finish(client);
    return 0;
}

/* Value of the property `name` (HasProperty child) of `node`; *out is empty when absent. */
static void read_property(UA_Client *client, const UA_NodeId *node, const char *name, UA_Variant *out) {
    UA_Variant_init(out);
    UA_BrowseRequest req;
    UA_BrowseRequest_init(&req);
    UA_BrowseDescription bd;
    UA_BrowseDescription_init(&bd);
    bd.nodeId = *node;
    bd.browseDirection = UA_BROWSEDIRECTION_FORWARD;
    bd.referenceTypeId = UA_NODEID_NUMERIC(0, UA_NS0ID_HASPROPERTY);
    bd.includeSubtypes = true;
    bd.nodeClassMask = UA_NODECLASS_VARIABLE;
    bd.resultMask = UA_BROWSERESULTMASK_ALL;
    req.nodesToBrowse = &bd;
    req.nodesToBrowseSize = 1;
    UA_BrowseResponse resp = UA_Client_Service_browse(client, req);
    if(resp.responseHeader.serviceResult == UA_STATUSCODE_GOOD && resp.resultsSize == 1) {
        for(size_t r = 0; r < resp.results[0].referencesSize; r++) {
            UA_ReferenceDescription *ref = &resp.results[0].references[r];
            if(ref->browseName.name.length == strlen(name) &&
               memcmp(ref->browseName.name.data, name, strlen(name)) == 0) {
                UA_Client_readValueAttribute(client, ref->nodeId.nodeId, out);
                break;
            }
        }
    }
    UA_BrowseResponse_clear(&resp);
}

static int s_read(Buf *b) {
    Lines ids = read_lines(arg("nodes", NULL));
    UA_Client *client = connect_default();
    const UA_UInt32 attrs[] = {UA_ATTRIBUTEID_VALUE, UA_ATTRIBUTEID_DATATYPE, UA_ATTRIBUTEID_VALUERANK,
                               UA_ATTRIBUTEID_ARRAYDIMENSIONS, UA_ATTRIBUTEID_ACCESSLEVEL,
                               UA_ATTRIBUTEID_USERACCESSLEVEL, UA_ATTRIBUTEID_DESCRIPTION};
    const size_t nattrs = sizeof(attrs) / sizeof(attrs[0]);
    put(b, "{\"result\":{\"nodes\":[");
    for(size_t i = 0; i < ids.n; i++) {
        UA_NodeId node;
        UA_NodeId_parse(&node, UA_STRING(ids.items[i]));
        UA_ReadValueId rv[7];
        for(size_t a = 0; a < nattrs; a++) {
            UA_ReadValueId_init(&rv[a]);
            rv[a].nodeId = node;
            rv[a].attributeId = attrs[a];
        }
        UA_ReadRequest req;
        UA_ReadRequest_init(&req);
        req.nodesToRead = rv;
        req.nodesToReadSize = nattrs;
        req.timestampsToReturn = UA_TIMESTAMPSTORETURN_BOTH;
        UA_ReadResponse resp = UA_Client_Service_read(client, req);
        if(i) put(b, ",");
        put(b, "{\"id\":");
        put_cstr(b, ids.items[i]);
        if(resp.responseHeader.serviceResult != UA_STATUSCODE_GOOD || resp.resultsSize != nattrs) {
            put(b, ",\"status\":");
            put_status(b, resp.responseHeader.serviceResult);
            put(b, "}");
            UA_ReadResponse_clear(&resp);
            UA_NodeId_clear(&node);
            continue;
        }
        UA_DataValue *dv = resp.results;
#define GOOD_DV(k) (dv[k].hasValue && (!dv[k].hasStatus || dv[k].status == UA_STATUSCODE_GOOD))
        put(b, ",\"status\":");
        put_status(b, dv[0].hasStatus ? dv[0].status : UA_STATUSCODE_GOOD);
        put(b, ",\"value\":");
        put_variant(b, dv[0].hasValue ? &dv[0].value : NULL);
        put(b, ",\"source_ts\":");
        if(dv[0].hasSourceTimestamp) put_scalar(b, &UA_TYPES[UA_TYPES_DATETIME], &dv[0].sourceTimestamp);
        else put(b, "null");
        put(b, ",\"datatype\":");
        put_variant(b, GOOD_DV(1) ? &dv[1].value : NULL);
        put(b, ",\"valueRank\":");
        put_variant(b, GOOD_DV(2) ? &dv[2].value : NULL);
        put(b, ",\"arrayDims\":");
        put_variant(b, GOOD_DV(3) ? &dv[3].value : NULL);
        put(b, ",\"accessLevel\":");
        put_variant(b, GOOD_DV(4) ? &dv[4].value : NULL);
        put(b, ",\"userAccessLevel\":");
        put_variant(b, GOOD_DV(5) ? &dv[5].value : NULL);
        put(b, ",\"description\":");
        put_variant(b, GOOD_DV(6) && !UA_Variant_isEmpty(&dv[6].value) &&
                           ((UA_LocalizedText *)dv[6].value.data)->text.length > 0 ? &dv[6].value : NULL);
        UA_Variant prop;
        read_property(client, &node, "EURange", &prop);
        put(b, ",\"euRange\":");
        if(prop.type == &UA_TYPES[UA_TYPES_RANGE] && UA_Variant_isScalar(&prop)) {
            UA_Range *r = (UA_Range *)prop.data;
            put(b, "{\"low\":%.17g,\"high\":%.17g}", r->low, r->high);
        } else {
            put(b, "null");
        }
        UA_Variant_clear(&prop);
        read_property(client, &node, "EngineeringUnits", &prop);
        put(b, ",\"engineeringUnits\":");
        if(prop.type == &UA_TYPES[UA_TYPES_EUINFORMATION] && UA_Variant_isScalar(&prop)) {
            UA_EUInformation *eu = (UA_EUInformation *)prop.data;
            put(b, "{\"display\":");
            put_uastr(b, &eu->displayName.text);
            put(b, ",\"unitId\":%d,\"namespace\":", eu->unitId);
            put_uastr(b, &eu->namespaceUri);
            put(b, "}");
        } else {
            put(b, "null");
        }
        UA_Variant_clear(&prop);
        put(b, "}");
        UA_ReadResponse_clear(&resp);
        UA_NodeId_clear(&node);
    }
    put(b, "]}}");
    finish(client);
    return 0;
}

/* --- subscriptions -------------------------------------------------------------------------- */

typedef struct {
    size_t n;
    size_t *counts;           /* notifications per item */
    char **first;             /* JSON of the first value per item */
    int *changed;             /* item saw a value different from the first */
    Buf *values;              /* per-item JSON value list (deadband) */
    size_t total;
} Monitor;

static Monitor g_mon;

static void on_change(UA_Client *client, UA_UInt32 subId, void *subCtx, UA_UInt32 monId, void *monCtx,
                      UA_DataValue *value) {
    (void)client; (void)subId; (void)subCtx; (void)monId;
    size_t i = (size_t)(uintptr_t)monCtx;
    if(i >= g_mon.n) return;
    Buf one = {0};
    put_variant(&one, value->hasValue ? &value->value : NULL);
    put(&one, "%c", 0);
    g_mon.counts[i]++;
    g_mon.total++;
    if(!g_mon.first[i]) g_mon.first[i] = strdup(one.s);
    else if(strcmp(g_mon.first[i], one.s) != 0) g_mon.changed[i] = 1;
    if(g_mon.values) {
        Buf *vb = &g_mon.values[i];
        put(vb, vb->len ? ",%s" : "%s", one.s);
    }
    free(one.s);
}

static void monitor_init(size_t n, int keep_values) {
    g_mon.n = n;
    g_mon.counts = calloc(n, sizeof(size_t));
    g_mon.first = calloc(n, sizeof(char *));
    g_mon.changed = calloc(n, sizeof(int));
    g_mon.values = keep_values ? calloc(n, sizeof(Buf)) : NULL;
    g_mon.total = 0;
}

static UA_UInt32 create_subscription(UA_Client *client, double publishing_ms) {
    UA_CreateSubscriptionRequest req = UA_CreateSubscriptionRequest_default();
    req.requestedPublishingInterval = publishing_ms;
    req.requestedLifetimeCount = 600;
    req.requestedMaxKeepAliveCount = 20;
    UA_CreateSubscriptionResponse resp = UA_Client_Subscriptions_create(client, req, NULL, NULL, NULL);
    UA_UInt32 id = resp.responseHeader.serviceResult == UA_STATUSCODE_GOOD ? resp.subscriptionId : 0;
    UA_CreateSubscriptionResponse_clear(&resp);
    return id;
}

/* Create one monitored item per node id; item i's context is i (+ offset). filter may be NULL. */
static UA_CreateMonitoredItemsResponse create_items(UA_Client *client, UA_UInt32 subId, char **ids, size_t n,
                                                    size_t offset, double sampling,
                                                    const UA_DataChangeFilter *filter) {
    UA_MonitoredItemCreateRequest *items =
        (UA_MonitoredItemCreateRequest *)UA_Array_new(n, &UA_TYPES[UA_TYPES_MONITOREDITEMCREATEREQUEST]);
    void **ctx = calloc(n, sizeof(void *));
    UA_Client_DataChangeNotificationCallback *cbs = calloc(n, sizeof(*cbs));
    UA_Client_DeleteMonitoredItemCallback *dels = calloc(n, sizeof(*dels));
    for(size_t i = 0; i < n; i++) {
        UA_NodeId node;
        UA_NodeId_parse(&node, UA_STRING(ids[i]));
        items[i] = UA_MonitoredItemCreateRequest_default(node);   /* takes ownership of node */
        items[i].requestedParameters.samplingInterval = sampling;
        items[i].requestedParameters.queueSize = 10;
        if(filter)
            UA_ExtensionObject_setValueCopy(&items[i].requestedParameters.filter, (void *)filter,
                                            &UA_TYPES[UA_TYPES_DATACHANGEFILTER]);
        ctx[i] = (void *)(uintptr_t)(i + offset);
        cbs[i] = on_change;
    }
    UA_CreateMonitoredItemsRequest req;
    UA_CreateMonitoredItemsRequest_init(&req);
    req.subscriptionId = subId;
    req.timestampsToReturn = UA_TIMESTAMPSTORETURN_BOTH;
    req.itemsToCreate = items;
    req.itemsToCreateSize = n;
    UA_CreateMonitoredItemsResponse resp =
        UA_Client_MonitoredItems_createDataChanges(client, req, ctx, cbs, dels);
    UA_Array_delete(items, n, &UA_TYPES[UA_TYPES_MONITOREDITEMCREATEREQUEST]);
    free(ctx);
    free(cbs);
    free(dels);
    return resp;
}

static int s_subscribe(Buf *b) {
    Lines ids = read_lines(arg("nodes", NULL));
    double started = now_s();
    UA_Client *client = connect_default();
    monitor_init(ids.n, 0);
    UA_UInt32 subId = create_subscription(client, 500.0);
    UA_CreateMonitoredItemsResponse resp = create_items(client, subId, ids.items, ids.n, 0, 250.0, NULL);
    size_t ok = 0;
    Buf bad = {0};
    put(&bad, "%s", "");
    for(size_t i = 0; i < resp.resultsSize; i++) {
        if(resp.results[i].statusCode == UA_STATUSCODE_GOOD) ok++;
        else put(&bad, bad.len ? ",\"0x%08X\"" : "\"0x%08X\"", resp.results[i].statusCode);
    }
    if(resp.responseHeader.serviceResult != UA_STATUSCODE_GOOD)
        put(&bad, bad.len ? ",\"0x%08X\"" : "\"0x%08X\"", resp.responseHeader.serviceResult);
    UA_CreateMonitoredItemsResponse_clear(&resp);
    double deadline = now_s() + num("timeout", 30);
    size_t changed = 0, initial = 0;
    while(now_s() < deadline) {
        UA_Client_run_iterate(client, 100);
        changed = initial = 0;
        for(size_t i = 0; i < ids.n; i++) {
            changed += (size_t)g_mon.changed[i];
            initial += g_mon.counts[i] > 0;
        }
        if(changed >= ok) break;
    }
    put(b, "{\"result\":{\"requested\":%zu,\"created_ok\":%zu,\"created_bad\":[%s],\"initial\":%zu,"
           "\"changed\":%zu,\"notifications\":%zu,\"elapsed_ms\":%d}}",
        ids.n, ok, bad.s, initial, changed, g_mon.total, (int)((now_s() - started) * 1000));
    UA_Client_Subscriptions_deleteSingle(client, subId);
    finish(client);
    return 0;
}

static int s_deadband(Buf *b) {
    UA_Client *client = connect_default();
    char *node = (char *)arg("node", NULL);
    monitor_init(2, 1);
    UA_UInt32 subId = create_subscription(client, 100.0);
    UA_CreateMonitoredItemsResponse plain = create_items(client, subId, &node, 1, 0, 0.0, NULL);
    UA_DataChangeFilter filter;
    UA_DataChangeFilter_init(&filter);
    filter.trigger = UA_DATACHANGETRIGGER_STATUSVALUE;
    filter.deadbandType = UA_DEADBANDTYPE_ABSOLUTE;
    filter.deadbandValue = num("deadband", 5.0);
    UA_CreateMonitoredItemsResponse filtered = create_items(client, subId, &node, 1, 1, 0.0, &filter);
    UA_StatusCode s0 = plain.resultsSize ? plain.results[0].statusCode : plain.responseHeader.serviceResult;
    UA_StatusCode s1 = filtered.resultsSize ? filtered.results[0].statusCode : filtered.responseHeader.serviceResult;
    UA_CreateMonitoredItemsResponse_clear(&plain);
    UA_CreateMonitoredItemsResponse_clear(&filtered);
    double deadline = now_s() + num("duration", 6);
    while(now_s() < deadline) UA_Client_run_iterate(client, 50);
    put(b, "{\"result\":{\"create_status\":[\"0x%08X\",\"0x%08X\"],\"unfiltered\":[%s],\"filtered\":[%s]}}", s0, s1,
        g_mon.values[0].s ? g_mon.values[0].s : "", g_mon.values[1].s ? g_mon.values[1].s : "");
    UA_Client_Subscriptions_deleteSingle(client, subId);
    finish(client);
    return 0;
}

static int s_write(Buf *b) {
    UA_Client *client = connect_default();
    UA_NodeId node;
    UA_NodeId_parse(&node, UA_STRING((char *)arg("node", NULL)));
    UA_Variant before, after, v;
    UA_Variant_init(&before);
    UA_Variant_init(&after);
    UA_Client_readValueAttribute(client, node, &before);
    UA_Int16 i16 = 7;
    UA_Double dbl = 7.0;
    UA_Int32 i32 = 7;
    if(before.type == &UA_TYPES[UA_TYPES_INT16]) UA_Variant_setScalar(&v, &i16, &UA_TYPES[UA_TYPES_INT16]);
    else if(before.type == &UA_TYPES[UA_TYPES_DOUBLE]) UA_Variant_setScalar(&v, &dbl, &UA_TYPES[UA_TYPES_DOUBLE]);
    else UA_Variant_setScalar(&v, &i32, &UA_TYPES[UA_TYPES_INT32]);
    /* the service call: a ServiceFault and a per-node status both come back as this code */
    UA_WriteValue wv;
    UA_WriteValue_init(&wv);
    wv.nodeId = node;
    wv.attributeId = UA_ATTRIBUTEID_VALUE;
    wv.value.value = v;
    wv.value.hasValue = true;
    UA_WriteRequest req;
    UA_WriteRequest_init(&req);
    req.nodesToWrite = &wv;
    req.nodesToWriteSize = 1;
    UA_WriteResponse resp = UA_Client_Service_write(client, req);
    int fault = resp.responseHeader.serviceResult != UA_STATUSCODE_GOOD;
    UA_StatusCode code = fault ? resp.responseHeader.serviceResult
                               : (resp.resultsSize ? resp.results[0] : UA_STATUSCODE_BADUNEXPECTEDERROR);
    UA_WriteResponse_clear(&resp);
    UA_Client_readValueAttribute(client, node, &after);
    put(b, "{\"result\":{\"status\":\"0x%08X\",\"service_fault\":%s,\"before\":", code, fault ? "true" : "false");
    put_variant(b, &before);
    put(b, ",\"after\":");
    put_variant(b, &after);
    put(b, "}}");
    UA_Variant_clear(&before);
    UA_Variant_clear(&after);
    UA_NodeId_clear(&node);
    finish(client);
    return 0;
}

/* Reconnect: open62541 1.4 re-opens the SecureChannel and creates a new Session by itself while
 * UA_Client_run_iterate runs; its subscriptions do not survive a new Session, so they are
 * re-created here once the new Session is activated (what SCADA applications on this stack do). */
static int g_session_lost = 0, g_session_back = 0;

static void on_state(UA_Client *client, UA_SecureChannelState cs, UA_SessionState ss, UA_StatusCode rc) {
    (void)client; (void)cs; (void)rc;
    static int was_active = 0;
    if(ss == UA_SESSIONSTATE_ACTIVATED) {
        if(g_session_lost) g_session_back = 1;
        was_active = 1;
    } else if(was_active) {
        if(!g_session_lost) emit_event("disconnected");
        g_session_lost = 1;
        was_active = 0;
    }
}

static int s_reconnect(Buf *b) {
    Lines ids = read_lines(arg("nodes", NULL));
    g_state_cb = on_state;
    UA_Client *client = connect_default();
    monitor_init(ids.n, 0);
    UA_UInt32 subId = create_subscription(client, 250.0);
    UA_CreateMonitoredItemsResponse r = create_items(client, subId, ids.items, ids.n, 0, 250.0, NULL);
    UA_CreateMonitoredItemsResponse_clear(&r);
    double deadline = now_s() + num("timeout", 60);
    while(g_mon.total == 0 && now_s() < deadline) UA_Client_run_iterate(client, 50);
    emit_event("ready");
    size_t at_loss = 0;
    int recovered = 0, recreated = 0, lost_seen = 0;
    double lost_at = 0;
    double next_try = 0;
    int manual = 0;
    while(now_s() < deadline) {
        UA_Client_run_iterate(client, 50);
        if(g_session_lost && !lost_seen) { lost_seen = 1; lost_at = now_s(); at_loss = g_mon.total; }
        if(lost_seen && !g_session_back && now_s() > next_try) {
            /* The library retries while it has hope; once it gives up (connectStatus bad,
             * e.g. connection refused while the server is down) the application reconnects. */
            UA_SecureChannelState cs;
            UA_SessionState ss;
            UA_StatusCode st;
            UA_Client_getState(client, &cs, &ss, &st);
            if(st != UA_STATUSCODE_GOOD || cs == UA_SECURECHANNELSTATE_CLOSED) {
                UA_Client_disconnect(client);
                UA_Client_connect(client, arg("endpoint", NULL));
                manual++;
            }
            next_try = now_s() + 0.5;
        }
        if(g_session_back && !recreated) {
            UA_Client_Subscriptions_deleteSingle(client, subId);     /* drop the local leftover */
            subId = create_subscription(client, 250.0);
            if(subId) {
                r = create_items(client, subId, ids.items, ids.n, 0, 250.0, NULL);
                UA_CreateMonitoredItemsResponse_clear(&r);
                recreated = 1;
            }
        }
        if(lost_seen && g_mon.total > at_loss) { recovered = 1; break; }
    }
    put(b, "{\"result\":{\"disconnect_seen\":%s,\"recovered\":%s,\"recovery_ms\":", lost_seen ? "true" : "false",
        recovered ? "true" : "false");
    if(recovered) put(b, "%d", (int)((now_s() - lost_at) * 1000));
    else put(b, "null");
    put(b, ",\"mechanism\":\"%s\",\"app_reconnects\":%d,\"notifications_after\":%zu}}",
        recreated ? "recreate" : "library", manual, g_mon.total - at_loss);
    finish(client);
    return 0;
}

int main(int argc, char **argv) {
    for(int i = 1; i + 1 < argc && arg_count < MAX_ARGS; i += 2) {
        if(strncmp(argv[i], "--", 2) != 0) { fprintf(stderr, "unexpected %s\n", argv[i]); return 2; }
        arg_keys[arg_count] = argv[i] + 2;
        arg_vals[arg_count++] = argv[i + 1];
    }
    if(arg("cert", "")[0] && arg("key", "")[0]) {
        g_cert = load_file(arg("cert", NULL));
        g_key = load_file(arg("key", NULL));
    }
    const char *scenario = arg("scenario", NULL);
    Buf b = {0};
    int rc;
    if(strcmp(scenario, "endpoints") == 0) rc = s_endpoints(&b);
    else if(strcmp(scenario, "connect") == 0) rc = s_connect(&b);
    else if(strcmp(scenario, "browse") == 0) rc = s_browse(&b);
    else if(strcmp(scenario, "translate") == 0) rc = s_translate(&b);
    else if(strcmp(scenario, "read") == 0) rc = s_read(&b);
    else if(strcmp(scenario, "subscribe") == 0) rc = s_subscribe(&b);
    else if(strcmp(scenario, "deadband") == 0) rc = s_deadband(&b);
    else if(strcmp(scenario, "write") == 0) rc = s_write(&b);
    else if(strcmp(scenario, "reconnect") == 0) rc = s_reconnect(&b);
    else { fprintf(stderr, "unknown scenario %s\n", scenario); return 2; }
    emit(&b);
    return rc;
}
