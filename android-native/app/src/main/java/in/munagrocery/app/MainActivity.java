package in.munagrocery.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.view.Window;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
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

    private static final String HOME_URL = "https://munagrocery.in";
    private static final int FILE_CHOOSER_REQUEST = 1001;

    // Runtime permissions requested up-front in onCreate().
    private static final int PERMISSION_REQ_CODE = 101;
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
        WindowInsetsControllerCompat controller =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        if (controller != null) {
            controller.setAppearanceLightStatusBars(false);   // White text/icons on blue header
            controller.setAppearanceLightNavigationBars(true); // Dark icons on light nav
        }

        webView = new WebView(this);
        setContentView(webView);

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

        // Android OS runtime permission dialogs (Camera + GPS) up-front so the
        // WebView's geolocation / getUserMedia prompts can be granted instantly.
        ensureRuntimePermissions();

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
            webView.loadUrl(HOME_URL);
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

    /**
     * Resolve the WebView prompts that were held while the native dialog was
     * showing: grant what the user allowed, deny what they refused.
     */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
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

