package in.munagrocery.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import androidx.core.app.NotificationCompat;

/**
 * AlarmManager tick target for the live-order background poll.
 * Also handles the 3 notification QUICK-ACTION buttons (Accept / Reject /
 * Review) that appear on the new-order heads-up notification — exactly like
 * WhatsApp / Telegram reply-in-notification. Each action runs entirely in
 * this receiver (no WebView needed): it POSTs update_order_status /
 * update_order_alert to the Sheet webhook, then rewrites the tray entry so
 * the admin sees the result instantly.
 */
public class BackgroundOrderPollReceiver extends BroadcastReceiver {

    public static final String ACTION_ACCEPT = "in.munagrocery.app.ORDER_ACCEPT";
    public static final String ACTION_REJECT = "in.munagrocery.app.ORDER_REJECT";
    public static final String ACTION_REVIEW = "in.munagrocery.app.ORDER_REVIEW";
    public static final String EXTRA_ORDER_ID = "order_id";
    public static final String EXTRA_CUSTOMER = "customer";
    public static final String EXTRA_TOTAL = "total";
    public static final String EXTRA_DELIVERY_TYPE = "delivery_type";

    private static final String CHANNEL_ID = "munna_new_orders";

    @Override
    public void onReceive(Context context, Intent intent) {
        final PendingResult result = goAsync();
        new Thread(() -> {
            try {
                String action = intent == null ? null : intent.getAction();
                if (ACTION_ACCEPT.equals(action)
                        || ACTION_REJECT.equals(action)
                        || ACTION_REVIEW.equals(action)) {
                    handleQuickAction(context, intent, action);
                } else {
                    MainActivity.runBackgroundPoll(context);
                }
            } finally {
                result.finish();
            }
        }, "munna-order-poll").start();
    }

    /** Runs the Accept / Reject / Review button action entirely natively. */
    private static void handleQuickAction(Context ctx, Intent intent, String action) {
        String orderId = intent.getStringExtra(EXTRA_ORDER_ID);
        if (orderId == null || orderId.trim().isEmpty()) return;
        orderId = orderId.trim();

        android.content.SharedPreferences p =
                ctx.getSharedPreferences("munna_live_orders", Context.MODE_PRIVATE);
        String hook = p.getString("webhook_override", "");
        if (hook == null || hook.trim().isEmpty()) hook = MainActivity.DEFAULT_WEBHOOK;

        boolean ok = false;
        try {
            if (ACTION_REVIEW.equals(action)) {
                ok = postJson(ctx, hook,
                        "{\"action\":\"update_order_alert\",\"order_id\":\"" + jsonEscape(orderId)
                                + "\",\"alert\":\"Reviewed\"}");
            } else {
                String newStatus = ACTION_ACCEPT.equals(action) ? "accepted" : "rejected";
                ok = postJson(ctx, hook,
                        "{\"action\":\"update_order_status\",\"order_id\":\"" + jsonEscape(orderId)
                                + "\",\"status\":\"" + newStatus + "\"}");
                postJson(ctx, hook,
                        "{\"action\":\"update_order_alert\",\"order_id\":\"" + jsonEscape(orderId)
                                + "\",\"alert\":\"Reviewed\"}");
            }
        } catch (Exception ignored) { }

        String title, body;
        String cust = intent.getStringExtra(EXTRA_CUSTOMER);
        String total = intent.getStringExtra(EXTRA_TOTAL);
        String dtyp = intent.getStringExtra(EXTRA_DELIVERY_TYPE);
        String suffix = (cust == null || cust.isEmpty() ? "" : " · " + cust)
                + (total == null || total.isEmpty() ? "" : " · Rs." + total)
                + (dtyp == null || dtyp.isEmpty() ? "" : " · " + dtyp);
        if (ACTION_ACCEPT.equals(action)) {
            title = "\u2705 Accepted — Order #" + orderId;
            body = (ok ? "Customer ko confirm bhej diya gaya." : "Sheet par save nahi hua — dobara try karein.") + suffix;
        } else if (ACTION_REJECT.equals(action)) {
            title = "\uD83D\uDEAB Rejected — Order #" + orderId;
            body = (ok ? "Stock wapas aa gaya." : "Sheet par save nahi hua — dobara try karein.") + suffix;
        } else {
            title = "\uD83D\uDC40 Reviewing — Order #" + orderId;
            body = "App khul raha hai…" + suffix;
        }

        NotificationManager nm = (NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        ensureChannel(nm);

        try {
            MainActivity.deliverOrderToPage(ctx, orderId, title, body);
        } catch (Throwable ignored) { }

        if (ACTION_REVIEW.equals(action)) {
            try {
                Intent open = new Intent(ctx, MainActivity.class);
                open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                open.putExtra("munna_order_id", orderId);
                ctx.startActivity(open);
            } catch (Throwable ignored) { }
        }

        int req = orderId.hashCode();
        Notification n = new NotificationCompat.Builder(ctx, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setAutoCancel(true)
                .setDefaults(NotificationCompat.DEFAULT_ALL)
                .build();
        nm.notify(req, n);
    }

    private static boolean postJson(Context ctx, String hook, String body) {
        java.net.HttpURLConnection conn = null;
        try {
            conn = (java.net.HttpURLConnection) new java.net.URL(hook).openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setInstanceFollowRedirects(true);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json;charset=UTF-8");
            byte[] b = body.getBytes("UTF-8");
            conn.setFixedLengthStreamingMode(b.length);
            java.io.OutputStream os = conn.getOutputStream();
            os.write(b);
            os.close();
            int code = conn.getResponseCode();
            return code >= 200 && code < 300;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String jsonEscape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c == '\n') sb.append("\\n");
            else if (c == '\r') sb.append("\\r");
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        return sb.toString();
    }

    private static void ensureChannel(NotificationManager nm) {
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "New orders", NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Alerts for new customer orders");
        ch.enableVibration(true);
        ch.setVibrationPattern(new long[]{0, 500, 250, 500, 250, 700});
        ch.setLockscreenVisibility(NotificationCompat.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(ch);
    }
}
