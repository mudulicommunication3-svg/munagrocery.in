package in.munagrocery.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlarmManager;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.core.app.ActivityCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsControllerCompat;

/**
 * Standalone native WebView shell for MUNNA GROCERY.
 *
 * Independent of Chrome / Custom Tabs / Trusted Web Activity — everything runs
 * inside a plain android.webkit.WebView so the app owns the full window (no
 * URL bar, no "Running in Chrome" banner) and the hardware Back button routes
 * through a single, robust navigation handler.
 *
 * Camera + GPS are requested via the standard Android OS runtime permission
 * dialogs (API 23+) so the html5-qrcode scanner and the 3 KM Leaflet map work
 * without the user having to enable anything manually in Settings.
 */
public class MainActivity extends Activity {

    // Live Cloudflare Pages host — www is currently the only working domain.
    // The apex (https://munagrocery.in) still resolves to GitHub Pages 404.
    private static final String HOME_URL = "https://www.munagrocery.in";
    private static final String ORDER_NOTIFICATION_CHANNEL_ID = "munna_new_orders";
    private static final int FILE_CHOOSER_REQUEST = 1001;

    // Runtime permissions requested up-front in onCreate().
    private static final int PERMISSION_REQ_CODE = 101;
    private static final int NOTIFICATION_PERMISSION_REQ_CODE = 102;
    private static final String[] REQUIRED_PERMISSIONS = {
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.CAMERA
    };

    private WebView webView;
    private ValueCallback<Uri[]> filePathCallback;

    // WebView permission prompts that arrived BEFORE the native dialog result:
    // they are re-invoked from onRequestPermissionsResult once the user answers.
    private GeolocationPermissions.Callback pendingGeoCallback;
    private String pendingGeoOrigin;
    private PermissionRequest pendingWebPermissionRequest;

    // "FLAG" preferences: the background AlarmManager poller sets FLAG_ADMIN
    // while the JS admin session is active; the bridge exposes
    // window.MunnaOrderBridge.setAdminMode(true/false) from admin.js so the
    // poller starts/stops with the session. Opened-from-notification orders
    // are handed back to the page via consumePendingNotificationOrderId().
    private static final String PREFS_NAME = "munna_live_orders";
    private static final String FLAG_ADMIN = "admin_mode";
    private static final String FLAG_NEW_ORDER_ID = "pending_notification_order_id";
    private static final String FLAG_NEW_TITLE = "pending_notification_title";
    private static final String FLAG_NEW_BODY = "pending_notification_body";
    private static final String FLAG_NOTICE_AT = "last_native_notice_at";
    private static final String FLAG_POLL_AT = "last_background_poll_at";
    private static final long BACKGROUND_POLL_MS = 15000L;   // 15 s live poll
    private static final long MIN_NATIVE_NOTICE_MS = 8000L;  // debounce tray posts
    private static final String DEFAULT_WEBHOOK =
            "https://script.google.com/macros/s/AKfycbyBG-2536FNp4Kdux8GSBjKMyHjaSBWqSIaJpHDQ68GtzKpyUYR9x_N9QthMdvXPNhE/exec";

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // TRUE edge-to-edge: draw BEHIND the transparent system bars so the
        // WebView stretches 100% full height from the very top edge to the
        // very bottom edge — no artificial gray/white gaps anywhere.
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        getWindow().setStatusBarColor(android.graphics.Color.TRANSPARENT);
        getWindow().setNavigationBarColor(android.graphics.Color.TRANSPARENT);
        // Extend the layout into the display cutout (notch / punch-hole) on
        // API 28+ so the WebView fills the FULL physical screen edge-to-edge.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            getWindow().getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        WindowInsetsControllerCompat controller =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        if (controller != null) {
            controller.setAppearanceLightStatusBars(false);   // White text/icons on blue header
            controller.setAppearanceLightNavigationBars(true); // Dark icons on light nav
        }
        // ZERO padding on the content view / WebView — the web app fills 100%
        // of the physical screen height edge-to-edge. Do NOT add window
        // insets as padding; the page handles its own safe-area padding.

        webView = new WebView(this);
        setContentView(webView);

        // NATIVE SHARE BRIDGE: exposes window.MunnaShare.postImage(base64, name,
        // text) to the page so the Share button opens the OFFICIAL Android
        // share sheet (WhatsApp, Facebook, Gmail...) WITH the product photo
        // attached — even though plain WebView has no navigator.share/files.
        webView.addJavascriptInterface(new ShareBridge(), "MunnaShare");
        // LIVE-ORDER BRIDGE: exposes window.MunnaOrderBridge.* to the page —
        // system tray notifications (foreground + background) like WhatsApp/
        // Telegram, background AlarmManager polling, and notification-tap
        // handoff (pending order id / title / body for the alert overlay).
        webView.addJavascriptInterface(new OrderBridge(), "MunnaOrderBridge");

        // Silent keep-alive: raise our own process priority while the app is
        // backgrounded so Android/OEMs don't kill the WebView — the 15 s
        // AlarmManager live-order poll keeps running (auto-restarts unless
        // force-stopped). Started here rather than in setAdminMode so the
        // service is up before any admin flag is written.
        try {
            startService(new Intent(this, KeepAliveService.class));
        } catch (Exception ignored) { }

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setGeolocationEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(true);
        // 3 KM Leaflet map tiles + Google Fonts / CDNs load over https.
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        // Camera + notification permission dialogs (API 33+ notifications) so
        // the WebView can ask for and receive new-order alerts.
        ensureRuntimePermissions();
        ensureNotificationPermission();

        // Keep every navigation strictly inside this WebView (no Chrome/Tab).
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                // Same-site navigation stays in the WebView; external schemes
                // (tel:, mailto:, whatsapp:, intent:) go to the OS.
                if (url.startsWith("http://") || url.startsWith("https://")) {
                    view.loadUrl(url);
                    return true;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception ignored) {
                }
                return true;
            }
        });

        // Auto-grant geolocation (map) + camera + handle file choosers.
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                                                           GeolocationPermissions.Callback callback) {
                // GPS: only grant once the OS has FINE_LOCATION. If it is not
                // granted yet, show the system dialog and remember the callback
                // so it can be resolved in onRequestPermissionsResult.
                if (hasPermission(Manifest.permission.ACCESS_FINE_LOCATION)) {
                    callback.invoke(origin, true, false);
                } else {
                    pendingGeoOrigin = origin;
                    pendingGeoCallback = callback;
                    ActivityCompat.requestPermissions(MainActivity.this,
                            new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                                    Manifest.permission.ACCESS_COARSE_LOCATION},
                            PERMISSION_REQ_CODE);
                }
            }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                // Camera (html5-qrcode video stream): auto-grant when the OS
                // permission is present, otherwise raise the system dialog and
                // resolve the WebView request from onRequestPermissionsResult.
                runOnUiThread(() -> {
                    if (hasPermission(Manifest.permission.CAMERA)) {
                        request.grant(request.getResources());
                    } else {
                        pendingWebPermissionRequest = request;
                        ActivityCompat.requestPermissions(MainActivity.this,
                                new String[]{Manifest.permission.CAMERA},
                                PERMISSION_REQ_CODE);
                    }
                });
            }

            @Override
            public boolean onShowFileChooser(WebView view,
                                             ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (filePathCallback != null) filePathCallback.onReceiveValue(null);
                filePathCallback = callback;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                try {
                    startActivityForResult(Intent.createChooser(i, "Select file"),
                            FILE_CHOOSER_REQUEST);
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });

        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState);
        } else {
            // Verified App Link launch -> open THAT munagrocery.in page,
            // otherwise the normal home URL.
            String startUrl = deepLinkUrl(getIntent());
            webView.loadUrl(startUrl != null ? startUrl : HOME_URL);
        }
    }

    /**
     * ACTION_VIEW entry point: return the live www.munagrocery.in or apex
     * munagrocery.in https URL that launched us, or null for a normal launcher
     * start (so we fall back to HOME_URL).
     */
    private String deepLinkUrl(Intent intent) {
        try {
            Uri data = (intent != null) ? intent.getData() : null;
            if (data != null && "https".equalsIgnoreCase(data.getScheme())) {
                String host = data.getHost();
                if ("munagrocery.in".equalsIgnoreCase(host)
                        || "www.munagrocery.in".equalsIgnoreCase(host)) {
                    return data.toString();
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    // LaunchMode singleTask: a tap on another munagrocery.in link reuses this
    // activity, so the new URL arrives here instead of onCreate(). A
    // notification tap also lands here (or in onCreate) — stash its order id
    // so deliverPendingNotificationToPage() (onResume) can restore the overlay.
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String notifOrderId = intent != null ? intent.getStringExtra("munna_order_id") : null;
        if (notifOrderId != null && !notifOrderId.isEmpty()) {
            // Keep title/body already written by the poller/bridge — only the
            // id may have arrived via the Intent extra.
            android.content.SharedPreferences p =
                    getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            if (p.getString(FLAG_NEW_ORDER_ID, "").isEmpty()) {
                p.edit().putString(FLAG_NEW_ORDER_ID, notifOrderId).apply();
            }
        }
        String url = deepLinkUrl(intent);
        if (url != null && webView != null) {
            final String target = url;
            runOnUiThread(() -> webView.loadUrl(target));
        }
    }

    /** True when the given runtime permission is already granted by the OS. */
    private boolean hasPermission(String perm) {
        return ContextCompat.checkSelfPermission(this, perm)
                == PackageManager.PERMISSION_GRANTED;
    }

    /** Show the Android OS permission dialogs for Camera + GPS if needed. */
    private void ensureRuntimePermissions() {
        boolean missing = false;
        for (String p : REQUIRED_PERMISSIONS) {
            if (!hasPermission(p)) { missing = true; break; }
        }
        if (missing) {
            ActivityCompat.requestPermissions(this, REQUIRED_PERMISSIONS, PERMISSION_REQ_CODE);
        }
    }

    /** Create the new-order channel and request Android 13+ notification permission. */
    private void ensureNotificationPermission() {
        NotificationManager notificationManager = getSystemService(NotificationManager.class);
        if (notificationManager != null) {
            NotificationChannel channel = new NotificationChannel(
                    ORDER_NOTIFICATION_CHANNEL_ID,
                    "New orders",
                    NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription("Alerts for new customer orders");
            channel.enableVibration(true);
            channel.setVibrationPattern(new long[]{0, 500, 250, 500, 250, 700});
            notificationManager.createNotificationChannel(channel);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                !hasPermission(Manifest.permission.POST_NOTIFICATIONS)) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    NOTIFICATION_PERMISSION_REQ_CODE
            );
        }
    }

    /**
     * Resolve the WebView prompts that were held while the native dialog was
     * showing: grant what the user allowed, deny what they refused.
     */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == NOTIFICATION_PERMISSION_REQ_CODE) {
            // Notification permission dialog finished (Android 13+): if the
            // user granted, the native tray alerts work from now on — no
            // further action needed; the bridge re-checks permission lazily.
            return;
        }
        if (requestCode != PERMISSION_REQ_CODE) return;

        // GPS: resolve the pending geolocation prompt.
        if (pendingGeoCallback != null) {
            boolean granted = hasPermission(Manifest.permission.ACCESS_FINE_LOCATION);
            pendingGeoCallback.invoke(pendingGeoOrigin, granted, false);
            pendingGeoCallback = null;
            pendingGeoOrigin = null;
        }

        // Camera: grant the WebView's getUserMedia request if allowed.
        if (pendingWebPermissionRequest != null) {
            if (hasPermission(Manifest.permission.CAMERA)) {
                pendingWebPermissionRequest.grant(pendingWebPermissionRequest.getResources());
            } else {
                pendingWebPermissionRequest.deny();
            }
            pendingWebPermissionRequest = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQUEST && filePathCallback != null) {
            Uri[] results = null;
            if (resultCode == RESULT_OK && data != null && data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
        }
    }

    // Bridge: JS ଫଟୋ base64 ପଠାଇଲେ ଅଫିସିଆଲ୍ ACTION_SEND ସିଟ୍ ଖୋଲେ —
    // ପ୍ରୋଡକ୍ଟ ଫଟୋ + ଟେକ୍ସଟ୍ + ଲିଙ୍କ୍ ସହ (WhatsApp ରେ ରିଚ୍ ପ୍ରିଭ୍ୟୁ ଆସେ)।
    class ShareBridge {
        @android.webkit.JavascriptInterface
        public void postImage(final String base64Data, final String fileName, final String shareText) {
            runOnUiThread(() -> {
                try {
                    byte[] imgBytes = android.util.Base64.decode(base64Data, android.util.Base64.DEFAULT);
                    java.io.File cacheDir = new java.io.File(getCacheDir(), "share");
                    if (!cacheDir.exists()) cacheDir.mkdirs();
                    String safe = (fileName == null || fileName.isEmpty()) ? "product.jpg" : fileName.replaceAll("[^a-zA-Z0-9._-]", "_");
                    java.io.File outFile = new java.io.File(cacheDir, safe);
                    java.io.FileOutputStream fos = new java.io.FileOutputStream(outFile);
                    fos.write(imgBytes);
                    fos.close();
                    Uri contentUri = FileProvider.getUriForFile(MainActivity.this, getPackageName() + ".fileprovider", outFile);
                    Intent send = new Intent(Intent.ACTION_SEND);
                    send.setType("image/jpeg");
                    send.putExtra(Intent.EXTRA_STREAM, contentUri);
                    send.putExtra(Intent.EXTRA_TEXT, shareText == null ? "" : shareText);
                    send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    startActivity(Intent.createChooser(send, "Share via"));
                } catch (Exception e) {
                    // Fallback: ଫଟୋ ବିନା ଖାଲି ଟେକ୍ସଟ୍ ସେୟାର କର
                    try {
                        Intent send = new Intent(Intent.ACTION_SEND);
                        send.setType("text/plain");
                        send.putExtra(Intent.EXTRA_TEXT, shareText == null ? "" : shareText);
                        startActivity(Intent.createChooser(send, "Share via"));
                    } catch (Exception ignored) { }
                }
            });
        }
    }

    // =========================================================================
    // LIVE-ORDER BRIDGE (window.MunnaOrderBridge) — called from js/admin.js.
    // =========================================================================
    class OrderBridge {

        @android.webkit.JavascriptInterface
        public boolean ensurePermission() {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                    && !hasPermission(Manifest.permission.POST_NOTIFICATIONS)) {
                runOnUiThread(() -> ensureNotificationPermission());
                return false;
            }
            return true;
        }

        @android.webkit.JavascriptInterface
        public void setAdminMode(final boolean enabled) {
            getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                    .edit().putBoolean(FLAG_ADMIN, enabled).apply();
            if (enabled) scheduleBackgroundPoll();
            else cancelBackgroundPoll();
        }

        @android.webkit.JavascriptInterface
        public void showNotification(final String title, final String body,
                                     final String orderId) {
            showNativeOrderNotification(title, body, orderId, false);
        }

        @android.webkit.JavascriptInterface
        public void cancelNotification(final String orderId) {
            try {
                NotificationManager nm = getSystemService(NotificationManager.class);
                if (nm != null) nm.cancel(orderId.hashCode());
                getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                        .edit()
                        .remove(FLAG_NEW_ORDER_ID)
                        .remove(FLAG_NEW_TITLE)
                        .remove(FLAG_NEW_BODY)
                        .apply();
            } catch (Exception ignored) { }
        }

        @android.webkit.JavascriptInterface
        public String consumePendingNotificationOrderId() {
            try {
                android.content.SharedPreferences p =
                        getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
                String id = p.getString(FLAG_NEW_ORDER_ID, "");
                if (!id.isEmpty()) {
                    p.edit().remove(FLAG_NEW_ORDER_ID)
                            .remove(FLAG_NEW_TITLE)
                            .remove(FLAG_NEW_BODY).apply();
                }
                return id;
            } catch (Exception e) { return ""; }
        }

        @android.webkit.JavascriptInterface
        public long lastBackgroundPollAt() {
            return getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                    .getLong(FLAG_POLL_AT, 0L);
        }
    }

    /**
     * Post a system-tray new-order notification (WhatsApp/Telegram style):
     * default channel sound + vibration, high importance (heads-up on
     * Android 8+), PendingIntent re-opens THIS app so the alert overlay is
     * restored. Debounced by MIN_NATIVE_NOTICE_MS unless triggered by the
     * background poller.
     */
    void showNativeOrderNotification(String title, String body, String orderId,
                                     boolean fromBackgroundPoll) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                    && !hasPermission(Manifest.permission.POST_NOTIFICATIONS)) {
                return;
            }
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm == null) return;
            NotificationChannel channel = new NotificationChannel(
                    ORDER_NOTIFICATION_CHANNEL_ID, "New orders",
                    NotificationManager.IMPORTANCE_HIGH);
            channel.setDescription("Alerts for new customer orders");
            channel.enableVibration(true);
            channel.setVibrationPattern(new long[]{0, 500, 250, 500, 250, 700});
            nm.createNotificationChannel(channel);

            android.content.SharedPreferences p =
                    getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            long now = System.currentTimeMillis();
            if (!fromBackgroundPoll
                    && now - p.getLong(FLAG_NOTICE_AT, 0L) < MIN_NATIVE_NOTICE_MS) {
                return;
            }
            p.edit().putLong(FLAG_NOTICE_AT, now)
                    .putString(FLAG_NEW_ORDER_ID, orderId == null ? "" : orderId)
                    .putString(FLAG_NEW_TITLE, title == null ? "" : title)
                    .putString(FLAG_NEW_BODY, body == null ? "" : body)
                    .apply();
            postTrayNotification(this, nm, title, body, orderId);
        } catch (Exception ignored) { }
    }

    /** Shared tray-post body used by both foreground bridge and bg poller. */
    private static void postTrayNotification(Context ctx, NotificationManager nm,
                                             String title, String body,
                                             String orderId) {
        try {
            Intent open = new Intent(ctx, MainActivity.class);
            open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
            open.putExtra("munna_order_id", orderId == null ? "" : orderId);
            int req = (orderId == null || orderId.isEmpty())
                    ? (int) System.currentTimeMillis() : orderId.hashCode();
            PendingIntent pi = PendingIntent.getActivity(
                    ctx, req, open,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            NotificationCompat.Builder nb = new NotificationCompat.Builder(
                    ctx, ORDER_NOTIFICATION_CHANNEL_ID)
                    .setSmallIcon(R.mipmap.ic_launcher)
                    .setContentTitle(title == null ? "New order" : title)
                    .setContentText(body == null ? "" : body)
                    .setStyle(new NotificationCompat.BigTextStyle()
                            .bigText(body == null ? "" : body))
                    .setPriority(NotificationCompat.PRIORITY_MAX)
                    .setCategory(NotificationCompat.CATEGORY_ALARM)
                    .setAutoCancel(true)
                    .setDefaults(NotificationCompat.DEFAULT_ALL)
                    .setContentIntent(pi);
            nm.notify(req, nb.build());
        } catch (Exception ignored) { }
    }

    // -------------------------------------------------------------------------
    // BACKGROUND POLLING (AlarmManager, 15 s) — keeps catching live orders
    // while the app is backgrounded WITHOUT a foreground service (which OEMs
    // kill aggressively). The JS admin session is the single source of truth:
    // js/admin.js calls setAdminMode(true) when ring polling starts and false
    // on logout — customer devices NEVER poll natively.
    // -------------------------------------------------------------------------
    private static final int BACKGROUND_POLL_REQ = 7701;

    void scheduleBackgroundPoll() {
        try {
            AlarmManager am = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
            if (am == null) return;
            am.setInexactRepeating(
                    AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    SystemClock.elapsedRealtime() + BACKGROUND_POLL_MS,
                    BACKGROUND_POLL_MS, backgroundPollPending());
        } catch (Exception ignored) { }
    }

    void cancelBackgroundPoll() {
        try {
            AlarmManager am = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
            if (am != null) am.cancel(backgroundPollPending());
        } catch (Exception ignored) { }
    }

    private PendingIntent backgroundPollPending() {
        Intent i = new Intent(this, BackgroundOrderPollReceiver.class);
        return PendingIntent.getBroadcast(
                this, BACKGROUND_POLL_REQ, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /**
     * Called by BackgroundOrderPollReceiver every alarm tick: GET_UNSEEN_ORDERS
     * from the Apps Script webhook; on a fresh pending+Unseen order posts the
     * SAME system-tray notification the foreground bridge posts. Skips the
     * debounce — the receiver only runs while the admin flag is on.
     */
    static void runBackgroundPoll(Context ctx) {
        try {
            android.content.SharedPreferences p =
                    ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
            if (!p.getBoolean(FLAG_ADMIN, false)) return; // not admin -> no poll
            String hook = p.getString("webhook_override", "");
            if (hook == null || hook.trim().isEmpty()) hook = DEFAULT_WEBHOOK;
            long now = System.currentTimeMillis();
            p.edit().putLong(FLAG_POLL_AT, now).apply();

            String url = hook + (hook.contains("?") ? "&" : "?")
                    + "action=GET_UNSEEN_ORDERS&_=" + now;
            java.net.HttpURLConnection conn = null;
            try {
                conn = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(8000);
                conn.setInstanceFollowRedirects(true);
                conn.setRequestMethod("GET");
                if (conn.getResponseCode() != 200) return;
                java.io.InputStream in = conn.getInputStream();
                java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[4096];
                int n;
                while ((n = in.read(buf)) != -1) bos.write(buf, 0, n);
                in.close();
                String json = bos.toString("UTF-8");

                org.json.JSONObject root = new org.json.JSONObject(json);
                org.json.JSONArray orders = root.optJSONArray("orders");
                if (orders == null) return;
                for (int i = 0; i < orders.length(); i++) {
                    org.json.JSONObject o = orders.optJSONObject(i);
                    if (o == null) continue;
                    String id = o.optString("order_id", o.optString("id", ""));
                    if (id.isEmpty()) continue;
                    if (!"pending".equalsIgnoreCase(o.optString("status", ""))) continue;
                    if ("Reviewed".equalsIgnoreCase(o.optString("admin_alert_status", ""))) continue;
                    String lastId = p.getString("last_bg_order_id", "");
                    long lastAt = p.getLong("last_bg_order_at", 0L);
                    if (id.equals(lastId) && now - lastAt < 60000L) continue;
                    p.edit().putString("last_bg_order_id", id)
                            .putLong("last_bg_order_at", now).apply();

                    String name = o.optString("customer_name", o.optString("name", "Customer"));
                    String total = o.optString("total", "0");
                    String dtype = o.optString("delivery_type", "");
                    String msg = name + " · Rs." + total
                            + ("Store Pickup".equals(dtype) ? " · Store Pickup" : " · Home Delivery");
                    p.edit().putString(FLAG_NEW_ORDER_ID, id)
                            .putString(FLAG_NEW_TITLE, "🚨 New Order #" + id)
                            .putString(FLAG_NEW_BODY, msg)
                            .putLong(FLAG_NOTICE_AT, now)
                            .apply();
                    NotificationManager nm = (NotificationManager)
                            ctx.getSystemService(Context.NOTIFICATION_SERVICE);
                    if (nm != null) {
                        NotificationChannel ch = new NotificationChannel(
                                ORDER_NOTIFICATION_CHANNEL_ID, "New orders",
                                NotificationManager.IMPORTANCE_HIGH);
                        ch.setDescription("Alerts for new customer orders");
                        ch.enableVibration(true);
                        ch.setVibrationPattern(new long[]{0, 500, 250, 500, 250, 700});
                        nm.createNotificationChannel(ch);
                        postTrayNotification(ctx, nm, "🚨 New Order #" + id, msg, id);
                    }
                    break; // one alert per tick — next tick handles the next
                }
            } finally {
                if (conn != null) conn.disconnect();
            }
        } catch (Exception ignored) {
            // Network/parse failure: next alarm tick retries automatically.
        }
    }

    /**
     * Hand the pending (notification-tapped) order id/title/body back to the
     * page once the activity resumes: js/init.js defines
     * window.munnaOnNativeOrderNotice which re-opens the full-screen alert
     * overlay on the CURRENT screen and re-rings it, so a background -> tap
     * flow lands on a live overlay, not a stale tray entry.
     */
    private void deliverPendingNotificationToPage() {
        if (webView == null) return;
        try {
            android.content.SharedPreferences p =
                    getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            String id = p.getString(FLAG_NEW_ORDER_ID, "");
            if (id.isEmpty()) return;
            String title = p.getString(FLAG_NEW_TITLE, "");
            String body = p.getString(FLAG_NEW_BODY, "");
            String js = "javascript:(function(){try{" +
                    "if(window.munnaOnNativeOrderNotice){window.munnaOnNativeOrderNotice(" +
                    jsonString(id) + "," + jsonString(title) + "," + jsonString(body) + ");}" +
                    "}catch(e){}})();";
            runOnUiThread(() -> {
                if (webView != null) webView.evaluateJavascript(js, null);
            });
        } catch (Exception ignored) { }
    }

    private static String jsonString(String s) {
        if (s == null) return "\"\"";
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c == '\n') sb.append("\\n");
            else if (c == '\r') sb.append("\\r");
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        return sb.append('"').toString();
    }

    /**
     * Universal hardware Back handler: unwind the WebView's own history first
     * (which also drives the page's popstate-based screen/modal stack), and
     * only finish the activity when there is nothing left to go back to.
     */
    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        if (webView != null) webView.saveState(outState);
    }

    @Override
    protected void onPause() {
        if (webView != null) webView.onPause();
        super.onPause();
    }

    @Override
    protected void onResume() {
        if (webView != null) webView.onResume();
        // A notification-tap while backgrounded stores a pending order id —
        // hand it to the page NOW so it can restore/re-open the alert overlay
        // on whichever screen the user is on.
        deliverPendingNotificationToPage();
        super.onResume();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}

