package in.munagrocery.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Re-arm the 15 s background live-order poll after a device reboot —
 * AlarmManager alarms are cleared on reboot, so without this receiver the
 * background catch-up would silently stop until the app is next opened.
 * No-ops unless the persisted admin flag is on (customer devices untouched).
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !"android.intent.action.QUICKBOOT_POWERON".equals(action)) {
            return;
        }
        try {
            boolean admin = context.getSharedPreferences("munna_live_orders", Context.MODE_PRIVATE)
                    .getBoolean("admin_mode", false);
            if (!admin) return;
            // Schedule via a tiny throwaway MainActivity-style helper path:
            // AlarmManager wiring lives on MainActivity; reuse its statics.
            try {
                Intent svc = new Intent(context, KeepAliveService.class);
                context.startService(svc);
            } catch (Exception ignored) {
            }
            // Re-arm the repeating alarm (uses MainActivity's static wiring).
            try {
                Intent i = new Intent(context, BackgroundOrderPollReceiver.class);
                android.app.PendingIntent pi = android.app.PendingIntent.getBroadcast(
                        context, 7701, i,
                        android.app.PendingIntent.FLAG_UPDATE_CURRENT
                                | android.app.PendingIntent.FLAG_IMMUTABLE);
                android.app.AlarmManager am = (android.app.AlarmManager)
                        context.getSystemService(Context.ALARM_SERVICE);
                if (am != null) {
                    am.setInexactRepeating(
                            android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP,
                            android.os.SystemClock.elapsedRealtime() + 15000L,
                            15000L, pi);
                }
            } catch (Exception ignored) {
            }
        } catch (Exception ignored) {
        }
    }
}