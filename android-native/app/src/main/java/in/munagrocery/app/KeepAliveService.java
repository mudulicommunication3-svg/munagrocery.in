package in.munagrocery.app;

import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

/**
 * Silent keep-alive: START_STICKY keeps the WebView process resident while
 * the app is backgrounded (auto-restarted unless the user force-stops it), so
 * the 15 s AlarmManager live-order poll keeps running like WhatsApp. No
 * foreground notification is posted — targetSdk 36 may log a warning, but the
 * service's purpose here is only process priority, not a visible FGS.
 */
public class KeepAliveService extends Service {
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Do not promote to a visible foreground notification: silent keep-alive.
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}