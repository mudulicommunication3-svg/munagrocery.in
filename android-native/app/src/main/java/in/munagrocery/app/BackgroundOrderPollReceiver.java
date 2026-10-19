package in.munagrocery.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * AlarmManager tick target for the 15 s live-order background poll.
 * Registered in AndroidManifest.xml (exported=false). Wakes only to run
 * MainActivity.runBackgroundPoll, which no-ops unless the admin flag is on —
 * so customer devices consume no battery.
 */
public class BackgroundOrderPollReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        final PendingResult result = goAsync();
        new Thread(() -> {
            try {
                MainActivity.runBackgroundPoll(context);
            } finally {
                result.finish();
            }
        }, "munna-order-poll").start();
    }
}