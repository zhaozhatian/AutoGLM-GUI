import { createFileRoute } from '@tanstack/react-router';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  cancelPushBatch,
  getDevices,
  getPushBatch,
  getPushSettings,
  listPushBatches,
  listPushWebhooks,
  retriggerPushItem,
  updatePushSettings,
  type Device,
  type PushBatch,
  type PushItemStatus,
  type PushSettings,
  type PushWebhook,
} from '../api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Inbox, Loader2, RefreshCw, RotateCcw, XCircle } from 'lucide-react';
import { useTranslation } from '../lib/i18n-context';

export const Route = createFileRoute('/push')({
  component: PushComponent,
});

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200',
  dispatching: 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200',
  queued: 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200',
  running: 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-200',
  succeeded:
    'bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200',
  completed:
    'bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-200',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200',
  partial: 'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-200',
  cancelled:
    'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
};

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '-';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

export function PushComponent() {
  const t = useTranslation();
  const [tab, setTab] = useState('batches');
  const [batches, setBatches] = useState<PushBatch[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PushBatch | null>(null);
  const [webhooks, setWebhooks] = useState<PushWebhook[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [settingsDraft, setSettingsDraft] = useState<PushSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busyItemId, setBusyItemId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadBatches = useCallback(async () => {
    try {
      const data = await listPushBatches({ limit: 100 });
      setBatches(data.batches);
      if (data.batches.length === 0) {
        setDetail(null);
        setWebhooks([]);
      }
      setSelectedId(current => {
        if (current && data.batches.some(batch => batch.id === current)) {
          return current;
        }
        return data.batches[0]?.id ?? null;
      });
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  const loadDetail = useCallback(async (batchId: string) => {
    try {
      const [batch, deliveries] = await Promise.all([
        getPushBatch(batchId),
        listPushWebhooks(batchId),
      ]);
      setDetail(batch);
      setWebhooks(deliveries);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  const loadDevices = useCallback(async () => {
    try {
      const data = await getDevices();
      setDevices(data);
    } catch {
      // Device list is only needed by the rules editor.
    }
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const data = await getPushSettings();
      setSettingsDraft(data);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => {
      setLoading(true);
      void Promise.all([loadBatches(), loadDevices(), loadSettings()]).finally(
        () => setLoading(false)
      );
    });
  }, [loadBatches, loadDevices, loadSettings]);

  useEffect(() => {
    if (!selectedId) {
      return;
    }
    queueMicrotask(() => {
      void loadDetail(selectedId);
    });
  }, [selectedId, loadDetail]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadBatches();
      if (selectedId) {
        void loadDetail(selectedId);
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadBatches, loadDetail, selectedId]);

  const statusLabel = useCallback(
    (status: string) =>
      (t.push.statuses as Record<string, string>)[status] ?? status,
    [t]
  );

  const handleRefresh = useCallback(async () => {
    setLoading(true);
    await Promise.all([loadBatches(), loadDevices()]);
    if (selectedId) {
      await loadDetail(selectedId);
    }
    setLoading(false);
  }, [loadBatches, loadDevices, loadDetail, selectedId]);

  const handleRetrigger = useCallback(
    async (itemId: string) => {
      if (!selectedId) {
        return;
      }
      setBusyItemId(itemId);
      try {
        await retriggerPushItem(selectedId, itemId);
        setNotice(t.push.retriggered);
        await Promise.all([loadDetail(selectedId), loadBatches()]);
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusyItemId(null);
      }
    },
    [selectedId, loadDetail, loadBatches, t]
  );

  const handleCancelBatch = useCallback(async () => {
    if (!selectedId || !window.confirm(t.push.cancelConfirm)) {
      return;
    }
    try {
      await cancelPushBatch(selectedId);
      setNotice(t.push.cancelled);
      await Promise.all([loadDetail(selectedId), loadBatches()]);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [selectedId, loadDetail, loadBatches, t]);

  const handleSaveSettings = useCallback(async () => {
    if (!settingsDraft) {
      return;
    }
    setSaving(true);
    try {
      const saved = await updatePushSettings({
        default_window_start: settingsDraft.default_window_start,
        default_window_end: settingsDraft.default_window_end,
        min_gap_seconds: settingsDraft.min_gap_seconds,
        offline_retry_seconds: settingsDraft.offline_retry_seconds,
        max_items_per_batch: settingsDraft.max_items_per_batch,
        default_callback_url: settingsDraft.default_callback_url,
        device_rules: settingsDraft.device_rules,
      });
      setSettingsDraft(saved);
      setNotice(t.push.saved);
      setError(null);
    } catch (err) {
      setError(`${t.push.saveFailed}: ${errorMessage(err)}`);
    } finally {
      setSaving(false);
    }
  }, [settingsDraft, t]);

  const orderedDevices = useMemo(() => {
    return [...devices].sort((left, right) => {
      const leftKey = left.display_name || left.serial;
      const rightKey = right.display_name || right.serial;
      return leftKey.localeCompare(rightKey);
    });
  }, [devices]);

  const updateRule = (
    serial: string,
    patch: Partial<PushSettings['device_rules'][string]>
  ) => {
    setSettingsDraft(current => {
      if (!current) {
        return current;
      }
      const existing = current.device_rules[serial] ?? {
        enabled: true,
        allowed_groups: [],
      };
      return {
        ...current,
        device_rules: {
          ...current.device_rules,
          [serial]: { ...existing, ...patch },
        },
      };
    });
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl space-y-6 p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold">{t.push.title}</h1>
            <p className="mt-1 text-sm text-slate-500">{t.push.subtitle}</p>
          </div>
          <Button variant="outline" onClick={handleRefresh} disabled={loading}>
            {loading ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="mr-2 h-4 w-4" />
            )}
            {t.push.refresh}
          </Button>
        </div>

        {error ? (
          <div className="rounded-md border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
            {error}
          </div>
        ) : null}
        {notice ? (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200">
            {notice}
          </div>
        ) : null}

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="batches">{t.push.tabBatches}</TabsTrigger>
            <TabsTrigger value="settings">{t.push.tabSettings}</TabsTrigger>
          </TabsList>

          <TabsContent value="batches" className="space-y-4">
            {batches.length === 0 ? (
              <Card>
                <CardContent className="flex flex-col items-center justify-center gap-2 py-16 text-center">
                  <Inbox className="h-8 w-8 text-slate-400" />
                  <p className="font-medium">{t.push.noBatches}</p>
                  <p className="max-w-md text-sm text-slate-500">
                    {t.push.noBatchesDesc}
                  </p>
                </CardContent>
              </Card>
            ) : (
              <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
                <div className="space-y-3">
                  {batches.map(batch => (
                    <button
                      key={batch.id}
                      type="button"
                      onClick={() => setSelectedId(batch.id)}
                      className={`w-full rounded-lg border p-3 text-left transition-colors ${
                        batch.id === selectedId
                          ? 'border-blue-400 bg-blue-50 dark:border-blue-700 dark:bg-blue-950'
                          : 'border-slate-200 bg-white hover:bg-slate-50 dark:border-slate-800 dark:bg-slate-950 dark:hover:bg-slate-900'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-medium">
                          {batch.external_batch_id}
                        </span>
                        <Badge className={STATUS_STYLES[batch.status]}>
                          {statusLabel(batch.status)}
                        </Badge>
                      </div>
                      <div className="mt-2 text-xs text-slate-500">
                        {formatDateTime(batch.created_at)}
                      </div>
                      <div className="mt-1 text-xs text-slate-500">
                        {t.push.itemCount}: {batch.item_count} ·{' '}
                        {t.push.successCount}: {batch.success_count} ·{' '}
                        {t.push.failureCount}: {batch.failure_count}
                      </div>
                    </button>
                  ))}
                </div>

                <Card>
                  <CardHeader className="flex flex-row items-center justify-between">
                    <CardTitle>{t.push.detail}</CardTitle>
                    {detail &&
                    (detail.status === 'pending' ||
                      detail.status === 'running') ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={handleCancelBatch}
                      >
                        <XCircle className="mr-2 h-4 w-4" />
                        {t.push.cancelBatch}
                      </Button>
                    ) : null}
                  </CardHeader>
                  <CardContent className="space-y-4">
                    {!detail ? (
                      <p className="py-10 text-center text-sm text-slate-500">
                        {t.push.selectBatch}
                      </p>
                    ) : (
                      <>
                        <dl className="grid grid-cols-2 gap-3 text-sm">
                          <div>
                            <dt className="text-slate-500">
                              {t.push.externalBatchId}
                            </dt>
                            <dd className="font-mono">
                              {detail.external_batch_id}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-500">{t.push.status}</dt>
                            <dd>
                              <Badge className={STATUS_STYLES[detail.status]}>
                                {statusLabel(detail.status)}
                              </Badge>
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-500">{t.push.window}</dt>
                            <dd>
                              {formatDateTime(detail.window_start)} →{' '}
                              {formatDateTime(detail.window_end)}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-500">
                              {t.push.callbackUrl}
                            </dt>
                            <dd className="truncate">
                              {detail.callback_url || '-'}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-slate-500">
                              {t.push.createdAt}
                            </dt>
                            <dd>{formatDateTime(detail.created_at)}</dd>
                          </div>
                          <div>
                            <dt className="text-slate-500">
                              {t.push.finishedAt}
                            </dt>
                            <dd>{formatDateTime(detail.finished_at)}</dd>
                          </div>
                        </dl>

                        <div className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-800">
                          <table className="w-full text-sm">
                            <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500 dark:bg-slate-900">
                              <tr>
                                <th className="px-3 py-2">{t.push.device}</th>
                                <th className="px-3 py-2">{t.push.group}</th>
                                <th className="px-3 py-2">
                                  {t.push.scheduledAt}
                                </th>
                                <th className="px-3 py-2">{t.push.status}</th>
                                <th className="px-3 py-2">{t.push.error}</th>
                                <th className="px-3 py-2">{t.push.task}</th>
                                <th className="px-3 py-2">{t.push.actions}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {(detail.items ?? []).map(item => (
                                <tr
                                  key={item.id}
                                  className="border-t border-slate-200 dark:border-slate-800"
                                >
                                  <td className="px-3 py-2 font-mono text-xs">
                                    {item.device_serial}
                                  </td>
                                  <td className="px-3 py-2">
                                    {item.group_name}
                                  </td>
                                  <td className="px-3 py-2 text-xs">
                                    {formatDateTime(item.scheduled_at)}
                                  </td>
                                  <td className="px-3 py-2">
                                    <Badge
                                      className={
                                        STATUS_STYLES[
                                          item.status as PushItemStatus
                                        ]
                                      }
                                    >
                                      {statusLabel(item.status)}
                                    </Badge>
                                  </td>
                                  <td
                                    className="max-w-[220px] truncate px-3 py-2 text-xs text-red-600"
                                    title={item.last_error || ''}
                                  >
                                    {item.last_error || '-'}
                                  </td>
                                  <td className="px-3 py-2 font-mono text-xs">
                                    {item.task_id || '-'}
                                  </td>
                                  <td className="px-3 py-2">
                                    {[
                                      'succeeded',
                                      'failed',
                                      'cancelled',
                                    ].includes(item.status) ? (
                                      <Button
                                        variant="ghost"
                                        size="sm"
                                        disabled={busyItemId === item.id}
                                        onClick={() => handleRetrigger(item.id)}
                                      >
                                        {busyItemId === item.id ? (
                                          <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                                        ) : (
                                          <RotateCcw className="mr-1 h-3 w-3" />
                                        )}
                                        {t.push.retrigger}
                                      </Button>
                                    ) : (
                                      '-'
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>

                        <div>
                          <h3 className="mb-2 text-sm font-medium">
                            {t.push.webhooks}
                          </h3>
                          {webhooks.length === 0 ? (
                            <p className="text-sm text-slate-500">
                              {t.push.noWebhooks}
                            </p>
                          ) : (
                            <div className="overflow-x-auto rounded-md border border-slate-200 dark:border-slate-800">
                              <table className="w-full text-sm">
                                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500 dark:bg-slate-900">
                                  <tr>
                                    <th className="px-3 py-2">event</th>
                                    <th className="px-3 py-2">status</th>
                                    <th className="px-3 py-2">
                                      {t.push.attempts}
                                    </th>
                                    <th className="px-3 py-2">
                                      {t.push.error}
                                    </th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {webhooks.map(delivery => (
                                    <tr
                                      key={delivery.id}
                                      className="border-t border-slate-200 dark:border-slate-800"
                                    >
                                      <td className="px-3 py-2 font-mono text-xs">
                                        {delivery.event}
                                      </td>
                                      <td className="px-3 py-2">
                                        {delivery.status}
                                      </td>
                                      <td className="px-3 py-2">
                                        {delivery.attempts}
                                      </td>
                                      <td className="max-w-[320px] truncate px-3 py-2 text-xs text-red-600">
                                        {delivery.last_error || '-'}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </CardContent>
                </Card>
              </div>
            )}
          </TabsContent>

          <TabsContent value="settings" className="space-y-4">
            {!settingsDraft ? (
              <div className="flex justify-center py-16">
                <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
              </div>
            ) : (
              <>
                <Card>
                  <CardHeader>
                    <CardTitle>{t.push.settingsTitle}</CardTitle>
                    <p className="text-sm text-slate-500">
                      {t.push.settingsDesc}
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-2">
                        <Label htmlFor="window-start">
                          {t.push.defaultWindowStart}
                        </Label>
                        <Input
                          id="window-start"
                          type="time"
                          value={settingsDraft.default_window_start}
                          onChange={event =>
                            setSettingsDraft({
                              ...settingsDraft,
                              default_window_start: event.target.value,
                            })
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="window-end">
                          {t.push.defaultWindowEnd}
                        </Label>
                        <Input
                          id="window-end"
                          type="time"
                          value={settingsDraft.default_window_end}
                          onChange={event =>
                            setSettingsDraft({
                              ...settingsDraft,
                              default_window_end: event.target.value,
                            })
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="min-gap">{t.push.minGapSeconds}</Label>
                        <Input
                          id="min-gap"
                          type="number"
                          min={0}
                          value={settingsDraft.min_gap_seconds}
                          onChange={event =>
                            setSettingsDraft({
                              ...settingsDraft,
                              min_gap_seconds: Number(event.target.value),
                            })
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="offline-retry">
                          {t.push.offlineRetrySeconds}
                        </Label>
                        <Input
                          id="offline-retry"
                          type="number"
                          min={5}
                          value={settingsDraft.offline_retry_seconds}
                          onChange={event =>
                            setSettingsDraft({
                              ...settingsDraft,
                              offline_retry_seconds: Number(event.target.value),
                            })
                          }
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="max-items">
                          {t.push.maxItemsPerBatch}
                        </Label>
                        <Input
                          id="max-items"
                          type="number"
                          min={1}
                          value={settingsDraft.max_items_per_batch}
                          onChange={event =>
                            setSettingsDraft({
                              ...settingsDraft,
                              max_items_per_batch: Number(event.target.value),
                            })
                          }
                        />
                      </div>
                      <div className="space-y-2 md:col-span-2">
                        <Label htmlFor="callback">
                          {t.push.defaultCallbackUrl}
                        </Label>
                        <Input
                          id="callback"
                          placeholder={t.push.callbackUrlPlaceholder}
                          value={settingsDraft.default_callback_url ?? ''}
                          onChange={event =>
                            setSettingsDraft({
                              ...settingsDraft,
                              default_callback_url:
                                event.target.value.trim() || null,
                            })
                          }
                        />
                      </div>
                    </div>
                    <p className="rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:bg-slate-900">
                      {t.push.ingestHint}
                    </p>
                    <div className="flex justify-end">
                      <Button onClick={handleSaveSettings} disabled={saving}>
                        {saving ? (
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        ) : null}
                        {saving ? t.push.saving : t.push.save}
                      </Button>
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle>{t.push.deviceRules}</CardTitle>
                    <p className="text-sm text-slate-500">
                      {t.push.deviceRulesDesc}
                    </p>
                  </CardHeader>
                  <CardContent>
                    {orderedDevices.length === 0 ? (
                      <p className="text-sm text-slate-500">
                        {t.push.noDevices}
                      </p>
                    ) : (
                      <div className="space-y-4">
                        {orderedDevices.map(device => {
                          const rule = settingsDraft.device_rules[
                            device.serial
                          ] ?? { enabled: true, allowed_groups: [] };
                          return (
                            <div
                              key={device.serial}
                              className="grid gap-3 rounded-md border border-slate-200 p-3 md:grid-cols-[220px_100px_1fr] dark:border-slate-800"
                            >
                              <div>
                                <div className="font-medium">
                                  {device.display_name ||
                                    device.model ||
                                    device.serial}
                                </div>
                                <div className="font-mono text-xs text-slate-500">
                                  {device.serial}
                                </div>
                                <div className="mt-1 text-xs text-slate-500">
                                  {device.state}
                                </div>
                              </div>
                              <div className="flex items-center gap-2">
                                <Switch
                                  checked={rule.enabled}
                                  onCheckedChange={checked =>
                                    updateRule(device.serial, {
                                      enabled: checked,
                                    })
                                  }
                                />
                                <span className="text-sm">
                                  {t.push.enabled}
                                </span>
                              </div>
                              <div className="space-y-1">
                                <Label
                                  htmlFor={`groups-${device.serial}`}
                                  className="text-xs text-slate-500"
                                >
                                  {t.push.allowedGroups}
                                </Label>
                                <Textarea
                                  id={`groups-${device.serial}`}
                                  rows={2}
                                  value={rule.allowed_groups.join('\n')}
                                  onChange={event =>
                                    updateRule(device.serial, {
                                      allowed_groups: event.target.value
                                        .split('\n')
                                        .map(name => name.trim())
                                        .filter(Boolean),
                                    })
                                  }
                                />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </CardContent>
                </Card>
              </>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
