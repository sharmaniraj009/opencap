package org.opencap.scada;

import static org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.Unsigned.uint;

import java.io.IOException;
import java.lang.reflect.Array;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.PrivateKey;
import java.security.Security;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import org.bouncycastle.jce.provider.BouncyCastleProvider;
import org.eclipse.milo.opcua.sdk.client.OpcUaClient;
import org.eclipse.milo.opcua.sdk.client.SessionActivityListener;
import org.eclipse.milo.opcua.sdk.client.api.UaSession;
import org.eclipse.milo.opcua.sdk.client.api.identity.AnonymousProvider;
import org.eclipse.milo.opcua.sdk.client.api.identity.IdentityProvider;
import org.eclipse.milo.opcua.sdk.client.api.identity.UsernameProvider;
import org.eclipse.milo.opcua.sdk.client.api.subscriptions.UaMonitoredItem;
import org.eclipse.milo.opcua.sdk.client.api.subscriptions.UaSubscription;
import org.eclipse.milo.opcua.sdk.client.api.subscriptions.UaSubscriptionManager;
import org.eclipse.milo.opcua.stack.client.DiscoveryClient;
import org.eclipse.milo.opcua.stack.core.AttributeId;
import org.eclipse.milo.opcua.stack.core.Identifiers;
import org.eclipse.milo.opcua.stack.core.UaException;
import org.eclipse.milo.opcua.stack.core.types.builtin.ByteString;
import org.eclipse.milo.opcua.stack.core.types.builtin.DataValue;
import org.eclipse.milo.opcua.stack.core.types.builtin.DateTime;
import org.eclipse.milo.opcua.stack.core.types.builtin.ExpandedNodeId;
import org.eclipse.milo.opcua.stack.core.types.builtin.ExtensionObject;
import org.eclipse.milo.opcua.stack.core.types.builtin.LocalizedText;
import org.eclipse.milo.opcua.stack.core.types.builtin.NodeId;
import org.eclipse.milo.opcua.stack.core.types.builtin.QualifiedName;
import org.eclipse.milo.opcua.stack.core.types.builtin.StatusCode;
import org.eclipse.milo.opcua.stack.core.types.builtin.Variant;
import org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.UInteger;
import org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.ULong;
import org.eclipse.milo.opcua.stack.core.types.builtin.unsigned.UShort;
import org.eclipse.milo.opcua.stack.core.types.enumerated.BrowseDirection;
import org.eclipse.milo.opcua.stack.core.types.enumerated.BrowseResultMask;
import org.eclipse.milo.opcua.stack.core.types.enumerated.DataChangeTrigger;
import org.eclipse.milo.opcua.stack.core.types.enumerated.DeadbandType;
import org.eclipse.milo.opcua.stack.core.types.enumerated.MessageSecurityMode;
import org.eclipse.milo.opcua.stack.core.types.enumerated.MonitoringMode;
import org.eclipse.milo.opcua.stack.core.types.enumerated.NodeClass;
import org.eclipse.milo.opcua.stack.core.types.enumerated.TimestampsToReturn;
import org.eclipse.milo.opcua.stack.core.types.structured.BrowseDescription;
import org.eclipse.milo.opcua.stack.core.types.structured.BrowsePath;
import org.eclipse.milo.opcua.stack.core.types.structured.BrowsePathResult;
import org.eclipse.milo.opcua.stack.core.types.structured.BrowsePathTarget;
import org.eclipse.milo.opcua.stack.core.types.structured.BrowseResult;
import org.eclipse.milo.opcua.stack.core.types.structured.DataChangeFilter;
import org.eclipse.milo.opcua.stack.core.types.structured.EUInformation;
import org.eclipse.milo.opcua.stack.core.types.structured.EndpointDescription;
import org.eclipse.milo.opcua.stack.core.types.structured.MonitoredItemCreateRequest;
import org.eclipse.milo.opcua.stack.core.types.structured.MonitoringParameters;
import org.eclipse.milo.opcua.stack.core.types.structured.Range;
import org.eclipse.milo.opcua.stack.core.types.structured.ReadValueId;
import org.eclipse.milo.opcua.stack.core.types.structured.ReferenceDescription;
import org.eclipse.milo.opcua.stack.core.types.structured.RelativePath;
import org.eclipse.milo.opcua.stack.core.types.structured.RelativePathElement;
import org.eclipse.milo.opcua.stack.core.types.structured.UserTokenPolicy;
import org.eclipse.milo.opcua.stack.core.types.structured.ViewDescription;

/**
 * Eclipse Milo scenario client for the L1 SCADA suite. Same argv and JSON protocol as
 * tests/scada/asyncua_client.py (the reference); see tests/scada/README.md. Prints JSON lines on
 * stdout, the last one {"result": {...}}.
 */
public final class L1Client {

    private static Map<String, String> args;
    private static X509Certificate certificate;
    private static KeyPair keyPair;

    public static void main(String[] argv) {
        System.setProperty("org.slf4j.simpleLogger.defaultLogLevel", "warn");
        System.setProperty("org.slf4j.simpleLogger.logFile", "System.err");
        // Aes256_Sha256_RsaPss needs SHA256withRSA/PSS, which the JDK does not provide: Milo's
        // examples (and Ignition) register BouncyCastle, a Milo dependency.
        Security.addProvider(new BouncyCastleProvider());
        args = parse(argv);
        int rc = 0;
        Object result;
        try {
            loadPki();
            result = run(arg("scenario", null));
        } catch (Throwable t) {
            t.printStackTrace();
            result = map("error", t.toString());
            rc = 1;
        }
        emit(map("result", result));
        System.exit(rc);
    }

    private static Object run(String scenario) throws Exception {
        switch (scenario) {
            case "endpoints": return endpoints();
            case "connect": return connectAll();
            case "browse": return browse();
            case "translate": return translate();
            case "read": return read();
            case "subscribe": return subscribe();
            case "deadband": return deadband();
            case "write": return write();
            case "reconnect": return reconnect();
            default: throw new IllegalArgumentException("unknown scenario " + scenario);
        }
    }

    // --- arguments, PKI, connections ----------------------------------------------------------

    private static Map<String, String> parse(String[] argv) {
        Map<String, String> out = new LinkedHashMap<>();
        for (int i = 0; i + 1 < argv.length; i += 2) {
            if (!argv[i].startsWith("--")) throw new IllegalArgumentException("unexpected " + argv[i]);
            out.put(argv[i].substring(2), argv[i + 1]);
        }
        return out;
    }

    private static String arg(String name, String dflt) {
        String v = args.get(name);
        if (v == null && dflt == null) throw new IllegalArgumentException("--" + name + " is required");
        return v == null ? dflt : v;
    }

    private static double num(String name, double dflt) {
        return Double.parseDouble(arg(name, Double.toString(dflt)));
    }

    private static int ns() {
        return Integer.parseInt(arg("ns", "2"));
    }

    private static List<String> lines(String name) throws IOException {
        List<String> out = new ArrayList<>();
        for (String l : Files.readAllLines(Path.of(arg(name, null)), StandardCharsets.UTF_8)) {
            if (!l.isBlank()) out.add(l);
        }
        return out;
    }

    private static void loadPki() throws Exception {
        if (args.get("cert") == null || args.get("key-der") == null) return;
        try (var in = Files.newInputStream(Path.of(args.get("cert")))) {
            certificate = (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(in);
        }
        PrivateKey key = KeyFactory.getInstance("RSA").generatePrivate(
            new PKCS8EncodedKeySpec(Files.readAllBytes(Path.of(args.get("key-der")))));
        keyPair = new KeyPair(certificate.getPublicKey(), key);
    }

    private static String policyUri(String name) {
        return "http://opcfoundation.org/UA/SecurityPolicy#" + name;
    }

    private static MessageSecurityMode mode(String name) {
        return MessageSecurityMode.valueOf(name);
    }

    private static IdentityProvider userIdentity() {
        return new UsernameProvider(arg("user", null), arg("password", ""));
    }

    /** A connected client on the endpoint with this policy/mode (default: --policy/--mode). */
    private static OpcUaClient connect(String policy, MessageSecurityMode mode, IdentityProvider identity)
            throws Exception {
        OpcUaClient client = OpcUaClient.create(
            arg("endpoint", null),
            eps -> eps.stream()
                .filter(e -> e.getSecurityPolicyUri().equals(policy) && e.getSecurityMode() == mode)
                .findFirst(),
            b -> {
                b.setApplicationName(LocalizedText.english("opencap scada-l1 Milo client"))
                    .setApplicationUri(arg("app-uri", "urn:opencap:scada-l1:client"))
                    .setProductUri("urn:opencap:scada-l1")
                    .setIdentityProvider(identity)
                    .setRequestTimeout(uint(10_000));
                if (certificate != null) b.setCertificate(certificate).setKeyPair(keyPair);
                return b.build();
            });
        client.connect().get(20, TimeUnit.SECONDS);
        return client;
    }

    private static OpcUaClient connectDefault() throws Exception {
        return connect(policyUri(arg("policy", "Basic256Sha256")), mode(arg("mode", "SignAndEncrypt")),
            userIdentity());
    }

    private static void close(OpcUaClient client) {
        try {
            client.disconnect().get(10, TimeUnit.SECONDS);
        } catch (Exception ignored) {
            // best effort
        }
    }

    // --- scenarios --------------------------------------------------------------------------

    private static Object endpoints() throws Exception {
        List<Object> out = new ArrayList<>();
        for (EndpointDescription e : DiscoveryClient.getEndpoints(arg("endpoint", null)).get(20, TimeUnit.SECONDS)) {
            Set<Integer> tokens = new java.util.TreeSet<>();
            if (e.getUserIdentityTokens() != null) {
                for (UserTokenPolicy p : e.getUserIdentityTokens()) tokens.add(p.getTokenType().getValue());
            }
            out.add(map("url", e.getEndpointUrl(), "policy", e.getSecurityPolicyUri(),
                "mode", e.getSecurityMode().getValue(), "tokens", new ArrayList<>(tokens),
                "server_cert", e.getServerCertificate() != null && e.getServerCertificate().isNotNull()
                    && e.getServerCertificate().length() > 0));
        }
        return map("endpoints", out);
    }

    private static Object connectAll() throws Exception {
        List<Object> attempts = new ArrayList<>();
        for (EndpointDescription e : DiscoveryClient.getEndpoints(arg("endpoint", null)).get(20, TimeUnit.SECONDS)) {
            String[][] tokens = {
                {"anonymous", null, null},
                {"username", arg("user", null), arg("password", "")},
                {"bad_password", arg("user", null), arg("password", "") + "-wrong"},
            };
            for (String[] t : tokens) {
                Map<String, Object> entry = map("policy", e.getSecurityPolicyUri(), "mode",
                    e.getSecurityMode().getValue(), "token", t[0], "ok", false, "state", null, "error", null);
                IdentityProvider id = t[1] == null ? new AnonymousProvider() : new UsernameProvider(t[1], t[2]);
                OpcUaClient client = null;
                try {
                    client = connect(e.getSecurityPolicyUri(), e.getSecurityMode(), id);
                    DataValue dv = client.readValue(0.0, TimestampsToReturn.Neither,
                        Identifiers.Server_ServerStatus_State).get(10, TimeUnit.SECONDS);
                    entry.put("ok", true);
                    entry.put("state", ((Number) dv.getValue().getValue()).intValue());
                } catch (Exception ex) {
                    entry.put("error", errorText(ex));
                } finally {
                    if (client != null) close(client);
                }
                attempts.add(entry);
            }
        }
        return map("attempts", attempts);
    }

    private static Object browse() throws Exception {
        OpcUaClient client = connectDefault();
        try {
            int ns = ns();
            UInteger maxRefs = uint(Long.parseLong(arg("max-refs", "0")));
            List<Object> nodes = new ArrayList<>();
            List<NodeId> queue = new ArrayList<>(List.of(Identifiers.ObjectsFolder));
            Set<NodeId> seen = new HashSet<>();
            int calls = 0;
            while (!queue.isEmpty()) {
                NodeId parent = queue.remove(0);
                BrowseDescription bd = new BrowseDescription(parent, BrowseDirection.Forward,
                    Identifiers.HierarchicalReferences, true,
                    uint(NodeClass.Object.getValue() | NodeClass.Variable.getValue()),
                    uint(BrowseResultMask.All.getValue()));
                BrowseResult result = client.browse(new ViewDescription(NodeId.NULL_VALUE, DateTime.MIN_VALUE, uint(0)), maxRefs, List.of(bd))
                    .get(20, TimeUnit.SECONDS).getResults()[0];
                calls++;
                List<ReferenceDescription> refs = new ArrayList<>(listOf(result.getReferences()));
                ByteString cp = result.getContinuationPoint();
                while (cp != null && cp.isNotNull() && cp.length() > 0) {
                    BrowseResult next = client.browseNext(false, List.of(cp)).get(20, TimeUnit.SECONDS)
                        .getResults()[0];
                    calls++;
                    refs.addAll(listOf(next.getReferences()));
                    cp = next.getContinuationPoint();
                }
                for (ReferenceDescription ref : refs) {
                    Optional<NodeId> id = ref.getNodeId().toNodeId(client.getNamespaceTable());
                    if (id.isEmpty() || id.get().getNamespaceIndex().intValue() != ns) continue;
                    nodes.add(map("id", id.get().toParseableString(), "name", ref.getBrowseName().toParseableString(),
                        "class", ref.getNodeClass().getValue(), "parent", parent.toParseableString()));
                    if (seen.add(id.get())) queue.add(id.get());
                }
            }
            return map("nodes", nodes, "browse_calls", calls);
        } finally {
            close(client);
        }
    }

    private static Object translate() throws Exception {
        List<String> paths = lines("paths");
        OpcUaClient client = connectDefault();
        try {
            int ns = ns();
            List<BrowsePath> bps = new ArrayList<>();
            for (String p : paths) {
                List<RelativePathElement> els = new ArrayList<>();
                for (String seg : p.split("/")) {
                    els.add(new RelativePathElement(Identifiers.HierarchicalReferences, false, true,
                        new QualifiedName(ns, seg)));
                }
                bps.add(new BrowsePath(Identifiers.ObjectsFolder,
                    new RelativePath(els.toArray(new RelativePathElement[0]))));
            }
            BrowsePathResult[] results = client.translateBrowsePaths(bps).get(20, TimeUnit.SECONDS).getResults();
            List<Object> out = new ArrayList<>();
            for (int i = 0; i < paths.size(); i++) {
                List<Object> targets = new ArrayList<>();
                for (BrowsePathTarget t : listOf(results[i].getTargets())) {
                    targets.add(t.getTargetId().toNodeId(client.getNamespaceTable())
                        .map(NodeId::toParseableString).orElse(t.getTargetId().toParseableString()));
                }
                out.add(map("path", paths.get(i), "status", hex(results[i].getStatusCode()), "targets", targets));
            }
            return map("results", out);
        } finally {
            close(client);
        }
    }

    private static Object property(OpcUaClient client, NodeId node, String name) throws Exception {
        BrowseDescription bd = new BrowseDescription(node, BrowseDirection.Forward, Identifiers.HasProperty, true,
            uint(NodeClass.Variable.getValue()), uint(BrowseResultMask.All.getValue()));
        BrowseResult result = client.browse(new ViewDescription(NodeId.NULL_VALUE, DateTime.MIN_VALUE, uint(0)), uint(0), List.of(bd))
            .get(20, TimeUnit.SECONDS).getResults()[0];
        for (ReferenceDescription ref : listOf(result.getReferences())) {
            if (!name.equals(ref.getBrowseName().getName())) continue;
            NodeId id = ref.getNodeId().toNodeId(client.getNamespaceTable()).orElseThrow();
            Object v = client.readValue(0.0, TimestampsToReturn.Neither, id).get(10, TimeUnit.SECONDS)
                .getValue().getValue();
            return v instanceof ExtensionObject ? ((ExtensionObject) v).decode(client.getStaticSerializationContext()) : v;
        }
        return null;
    }

    private static Object read() throws Exception {
        List<String> ids = lines("nodes");
        OpcUaClient client = connectDefault();
        try {
            AttributeId[] attrs = {AttributeId.Value, AttributeId.DataType, AttributeId.ValueRank,
                AttributeId.ArrayDimensions, AttributeId.AccessLevel, AttributeId.UserAccessLevel,
                AttributeId.Description};
            List<Object> out = new ArrayList<>();
            for (String text : ids) {
                NodeId node = NodeId.parse(text);
                List<ReadValueId> rvs = new ArrayList<>();
                for (AttributeId a : attrs) rvs.add(new ReadValueId(node, a.uid(), null, QualifiedName.NULL_VALUE));
                DataValue[] dv = client.read(0.0, TimestampsToReturn.Both, rvs).get(20, TimeUnit.SECONDS)
                    .getResults();
                Object eu = property(client, node, "EURange");
                Object units = property(client, node, "EngineeringUnits");
                Map<String, Object> r = map("id", text, "status", hex(dv[0].getStatusCode()),
                    "value", jsonValue(dv[0].getValue().getValue()),
                    "source_ts", dv[0].getSourceTime() == null ? null : dv[0].getSourceTime().getJavaDate().toInstant().toString(),
                    "datatype", good(dv[1]) ? ((NodeId) dv[1].getValue().getValue()).toParseableString() : null,
                    "valueRank", good(dv[2]) ? ((Number) dv[2].getValue().getValue()).intValue() : null,
                    "arrayDims", good(dv[3]) && dv[3].getValue().getValue() != null ? jsonValue(dv[3].getValue().getValue()) : null,
                    "accessLevel", good(dv[4]) ? ((Number) dv[4].getValue().getValue()).intValue() : null,
                    "userAccessLevel", good(dv[5]) ? ((Number) dv[5].getValue().getValue()).intValue() : null,
                    "description", good(dv[6]) && dv[6].getValue().getValue() != null
                        ? ((LocalizedText) dv[6].getValue().getValue()).getText() : null,
                    "euRange", eu instanceof Range ? map("low", ((Range) eu).getLow(), "high", ((Range) eu).getHigh()) : null,
                    "engineeringUnits", units instanceof EUInformation
                        ? map("display", ((EUInformation) units).getDisplayName().getText(),
                              "unitId", ((EUInformation) units).getUnitId(),
                              "namespace", ((EUInformation) units).getNamespaceUri())
                        : null);
                out.add(r);
            }
            return map("nodes", out);
        } finally {
            close(client);
        }
    }

    private static List<MonitoredItemCreateRequest> requests(List<String> ids, double sampling, ExtensionObject filter) {
        List<MonitoredItemCreateRequest> reqs = new ArrayList<>();
        for (int i = 0; i < ids.size(); i++) {
            ReadValueId rv = new ReadValueId(NodeId.parse(ids.get(i)), AttributeId.Value.uid(), null,
                QualifiedName.NULL_VALUE);
            MonitoringParameters mp = new MonitoringParameters(uint(i), sampling, filter, uint(10), true);
            reqs.add(new MonitoredItemCreateRequest(rv, MonitoringMode.Reporting, mp));
        }
        return reqs;
    }

    private static Object subscribe() throws Exception {
        List<String> ids = lines("nodes");
        long started = System.nanoTime();
        OpcUaClient client = connectDefault();
        try {
            Map<Integer, Set<String>> distinct = new ConcurrentHashMap<>();
            AtomicInteger notifications = new AtomicInteger();
            UaSubscription sub = client.getSubscriptionManager().createSubscription(500.0).get(20, TimeUnit.SECONDS);
            List<UaMonitoredItem> items = sub.createMonitoredItems(TimestampsToReturn.Both, requests(ids, 250.0, null),
                (item, idx) -> item.setValueConsumer(v -> {
                    notifications.incrementAndGet();
                    distinct.computeIfAbsent(item.getClientHandle().intValue(), k -> ConcurrentHashMap.newKeySet())
                        .add(json(jsonValue(v.getValue().getValue())));
                })).get(60, TimeUnit.SECONDS);
            List<Object> bad = new ArrayList<>();
            int okCount = 0;
            for (UaMonitoredItem item : items) {
                if (item.getStatusCode().isGood()) okCount++;
                else bad.add(hex(item.getStatusCode()));
            }
            long deadline = System.nanoTime() + (long) (num("timeout", 30) * 1e9);
            while (System.nanoTime() < deadline && changed(distinct) < okCount) Thread.sleep(100);
            Map<String, Object> r = map("requested", ids.size(), "created_ok", okCount, "created_bad", bad,
                "initial", distinct.size(), "changed", changed(distinct), "notifications", notifications.get(),
                "elapsed_ms", (System.nanoTime() - started) / 1_000_000);
            client.getSubscriptionManager().deleteSubscription(sub.getSubscriptionId()).get(10, TimeUnit.SECONDS);
            return r;
        } finally {
            close(client);
        }
    }

    private static int changed(Map<Integer, Set<String>> distinct) {
        int n = 0;
        for (Set<String> s : distinct.values()) if (s.size() >= 2) n++;
        return n;
    }

    private static Object deadband() throws Exception {
        OpcUaClient client = connectDefault();
        try {
            String node = arg("node", null);
            ExtensionObject filter = ExtensionObject.encode(client.getStaticSerializationContext(),
                new DataChangeFilter(DataChangeTrigger.StatusValue, uint(DeadbandType.Absolute.getValue()),
                    num("deadband", 5.0)));
            List<MonitoredItemCreateRequest> reqs = new ArrayList<>(requests(List.of(node), 0.0, null));
            reqs.addAll(requests(List.of(node, node), 0.0, filter).subList(1, 2));   // client handle 1
            Map<Integer, List<Object>> values = new ConcurrentHashMap<>();
            UaSubscription sub = client.getSubscriptionManager().createSubscription(100.0).get(20, TimeUnit.SECONDS);
            List<UaMonitoredItem> items = sub.createMonitoredItems(TimestampsToReturn.Both, reqs,
                (item, idx) -> item.setValueConsumer(v -> values.computeIfAbsent(item.getClientHandle().intValue(),
                    k -> Collections.synchronizedList(new ArrayList<>())).add(jsonValue(v.getValue().getValue()))))
                .get(20, TimeUnit.SECONDS);
            Thread.sleep((long) (num("duration", 6) * 1000));
            List<Object> status = new ArrayList<>();
            for (UaMonitoredItem item : items) status.add(hex(item.getStatusCode()));
            client.getSubscriptionManager().deleteSubscription(sub.getSubscriptionId()).get(10, TimeUnit.SECONDS);
            return map("create_status", status, "unfiltered", values.getOrDefault(0, List.of()),
                "filtered", values.getOrDefault(1, List.of()));
        } finally {
            close(client);
        }
    }

    private static Object write() throws Exception {
        OpcUaClient client = connectDefault();
        try {
            NodeId node = NodeId.parse(arg("node", null));
            Object before = jsonValue(client.readValue(0.0, TimestampsToReturn.Neither, node).get().getValue().getValue());
            NodeId type = (NodeId) client.read(0.0, TimestampsToReturn.Neither,
                List.of(new ReadValueId(node, AttributeId.DataType.uid(), null, QualifiedName.NULL_VALUE)))
                .get().getResults()[0].getValue().getValue();
            Object value = Identifiers.Int16.equals(type) ? (Object) (short) 7
                : Identifiers.UInt16.equals(type) ? (Object) UShort.valueOf(7)
                : Identifiers.Double.equals(type) ? (Object) 7.0 : (Object) 7;
            String status;
            boolean fault = false;
            try {
                List<StatusCode> codes = client.writeValues(List.of(node), List.of(DataValue.valueOnly(new Variant(value))))
                    .get(20, TimeUnit.SECONDS);
                status = hex(codes.get(0));
            } catch (ExecutionException ex) {
                status = errorText(ex);
                fault = true;
            }
            Object after = jsonValue(client.readValue(0.0, TimestampsToReturn.Neither, node).get().getValue().getValue());
            return map("status", status, "service_fault", fault, "before", before, "after", after);
        } finally {
            close(client);
        }
    }

    /**
     * Subscribe, signal ready, survive a server restart. Milo reconnects and re-activates by
     * itself; a subscription that cannot be transferred to the new session is re-created here,
     * as Ignition does (onSubscriptionTransferFailed).
     */
    private static Object reconnect() throws Exception {
        List<String> ids = lines("nodes");
        OpcUaClient client = connectDefault();
        AtomicInteger notes = new AtomicInteger();
        AtomicInteger notesAtLoss = new AtomicInteger(-1);
        AtomicBoolean disconnectSeen = new AtomicBoolean();
        AtomicBoolean transferFailed = new AtomicBoolean();
        AtomicLong lostAt = new AtomicLong();
        try {
            client.addSessionActivityListener(new SessionActivityListener() {
                @Override
                public void onSessionInactive(UaSession session) {
                    if (disconnectSeen.compareAndSet(false, true)) {
                        lostAt.set(System.nanoTime());
                        notesAtLoss.set(notes.get());
                        emit(map("event", "disconnected"));
                    }
                }

                @Override
                public void onSessionActive(UaSession session) {
                    if (disconnectSeen.get()) emit(map("event", "session_active"));
                }
            });
            client.getSubscriptionManager().addSubscriptionListener(new UaSubscriptionManager.SubscriptionListener() {
                @Override
                public void onSubscriptionTransferFailed(UaSubscription subscription, StatusCode statusCode) {
                    transferFailed.set(true);
                    new Thread(() -> {
                        try {
                            createReconnectSubscription(client, ids, notes);
                        } catch (Exception ex) {
                            ex.printStackTrace();
                        }
                    }).start();
                }
            });
            createReconnectSubscription(client, ids, notes);
            long deadline = System.nanoTime() + (long) (num("timeout", 60) * 1e9);
            while (notes.get() == 0 && System.nanoTime() < deadline) Thread.sleep(50);
            emit(map("event", "ready"));
            boolean recovered = false;
            while (System.nanoTime() < deadline) {
                if (disconnectSeen.get() && notes.get() > notesAtLoss.get()) {
                    recovered = true;
                    break;
                }
                Thread.sleep(100);
            }
            return map("disconnect_seen", disconnectSeen.get(), "recovered", recovered,
                "recovery_ms", recovered ? (System.nanoTime() - lostAt.get()) / 1_000_000 : null,
                "mechanism", transferFailed.get() ? "recreate" : "transfer",
                "notifications_after", Math.max(0, notes.get() - notesAtLoss.get()));
        } finally {
            close(client);
        }
    }

    private static void createReconnectSubscription(OpcUaClient client, List<String> ids, AtomicInteger notes)
            throws Exception {
        UaSubscription sub = client.getSubscriptionManager().createSubscription(250.0).get(20, TimeUnit.SECONDS);
        sub.createMonitoredItems(TimestampsToReturn.Both, requests(ids, 250.0, null),
            (item, idx) -> item.setValueConsumer(v -> notes.incrementAndGet())).get(20, TimeUnit.SECONDS);
    }

    // --- JSON ---------------------------------------------------------------------------------

    private static boolean good(DataValue dv) {
        return dv.getStatusCode() == null || dv.getStatusCode().isGood();
    }

    private static String hex(StatusCode code) {
        return String.format("0x%08X", code == null ? 0L : code.getValue());
    }

    private static String errorText(Throwable ex) {
        Throwable t = ex;
        while (t != null) {
            if (t instanceof UaException) return hex(((UaException) t).getStatusCode());
            t = t.getCause();
        }
        return ex.toString();
    }

    private static <T> List<T> listOf(T[] array) {
        return array == null ? List.of() : List.of(array);
    }

    private static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    /** A Variant value as plain JSON-able Java values (numbers, strings, booleans, lists). */
    private static Object jsonValue(Object v) {
        if (v == null) return null;
        if (v.getClass().isArray()) {
            List<Object> out = new ArrayList<>();
            for (int i = 0; i < Array.getLength(v); i++) out.add(jsonValue(Array.get(v, i)));
            return out;
        }
        if (v instanceof Float) return ((Float) v).doubleValue();
        if (v instanceof ULong) return ((ULong) v).toBigInteger();
        if (v instanceof Number || v instanceof Boolean || v instanceof String) return v;
        if (v instanceof LocalizedText) return ((LocalizedText) v).getText();
        if (v instanceof NodeId) return ((NodeId) v).toParseableString();
        if (v instanceof ExpandedNodeId) return ((ExpandedNodeId) v).toParseableString();
        if (v instanceof DateTime) return ((DateTime) v).getJavaDate().toInstant().toString();
        return v.toString();
    }

    private static synchronized void emit(Object obj) {
        System.out.println(json(obj));
        System.out.flush();
    }

    private static String json(Object o) {
        if (o == null) return "null";
        if (o instanceof String) return quote((String) o);
        if (o instanceof Boolean) return o.toString();
        if (o instanceof Double || o instanceof Float) {
            double d = ((Number) o).doubleValue();
            return Double.isFinite(d) ? Double.toString(d) : quote(Double.toString(d));
        }
        if (o instanceof Number) return o.toString();
        if (o instanceof Map) {
            StringBuilder sb = new StringBuilder("{");
            for (Map.Entry<?, ?> e : ((Map<?, ?>) o).entrySet()) {
                if (sb.length() > 1) sb.append(',');
                sb.append(quote(String.valueOf(e.getKey()))).append(':').append(json(e.getValue()));
            }
            return sb.append('}').toString();
        }
        if (o instanceof Iterable) {
            StringBuilder sb = new StringBuilder("[");
            for (Object e : (Iterable<?>) o) {
                if (sb.length() > 1) sb.append(',');
                sb.append(json(e));
            }
            return sb.append(']').toString();
        }
        return quote(o.toString());
    }

    private static String quote(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.append('"').toString();
    }

    private L1Client() {
    }
}
