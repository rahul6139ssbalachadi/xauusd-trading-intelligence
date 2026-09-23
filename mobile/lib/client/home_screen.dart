import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:fl_chart/fl_chart.dart';

import '../../services/auth_service.dart';
import '../../services/api_client.dart';
import '../../shared/theme.dart';
import '../../shared/widgets.dart';

class ClientHomeScreen extends ConsumerStatefulWidget {
  const ClientHomeScreen({super.key});

  @override
  ConsumerState<ClientHomeScreen> createState() => _ClientHomeScreenState();
}

class _ClientHomeScreenState extends ConsumerState<ClientHomeScreen> {
  int _selectedIndex = 0;

  @override
  Widget build(BuildContext context) {
    final user = ref.watch(authProvider).value;
    if (user == null) return const SizedBox.shrink();

    return Scaffold(
      backgroundColor: AppTheme.bgPrimary,
      appBar: AppBar(
        title: const Text('Trading Dashboard'),
        actions: [
          IconButton(
            icon: const Icon(Icons.notifications_outlined),
            onPressed: () {},
          ),
          IconButton(
            icon: const Icon(Icons.logout),
            onPressed: () => ref.read(authProvider.notifier).logout(),
          ),
        ],
      ),
      body: IndexedStack(
        index: _selectedIndex,
        children: const [
          _HomeTab(),
          _TradesTab(),
          _SettingsTab(),
        ],
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _selectedIndex,
        onTap: (i) => setState(() => _selectedIndex = i),
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.home_outlined),
            label: 'Home',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.swap_horiz),
            label: 'Trades',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.settings_outlined),
            label: 'Settings',
          ),
        ],
      ),
    );
  }
}

class _HomeTab extends ConsumerWidget {
  const _HomeTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);

    return FutureBuilder(
      future: api.get('/api/summary'),
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const LoadingIndicator();
        }

        if (snapshot.hasError) {
          return ErrorView(
            message: 'Failed to load data',
            onRetry: () => ref.refresh(apiProvider),
          );
        }

        final data = snapshot.data ?? {};
        final totalTrades = data['total_trades'] ?? 0;
        final winRate = (data['win_rate'] ?? 0.0) * 100;
        final profitFactor = data['profit_factor'] ?? 0.0;
        final netPnl = data['net_pnl_usd'] ?? 0.0;
        final maxDrawdown = data['max_drawdown_pct'] ?? 0.0;

        return SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Equity Curve
              AppCard(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Equity Curve',
                      style: GoogleFonts.inter(
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                        color: AppTheme.textPrimary,
                      ),
                    ),
                    const SizedBox(height: 16),
                    SizedBox(
                      height: 200,
                      child: _EquityChart(api: api),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),

              // Metrics Grid
              GridView.count(
                crossAxisCount: 2,
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                mainAxisSpacing: 12,
                crossAxisSpacing: 12,
                childAspectRatio: 1.5,
                children: [
                  MetricCard(
                    label: 'Total Trades',
                    value: '$totalTrades',
                    icon: Icons.swap_horiz,
                  ),
                  MetricCard(
                    label: 'Win Rate',
                    value: '${winRate.toStringAsFixed(1)}%',
                    icon: Icons.trending_up,
                    valueColor: winRate >= 50 ? AppTheme.accentPrimary : AppTheme.accentDanger,
                  ),
                  MetricCard(
                    label: 'Profit Factor',
                    value: profitFactor.toStringAsFixed(2),
                    icon: Icons.analytics,
                    valueColor: profitFactor > 1.5 ? AppTheme.accentPrimary : AppTheme.accentWarning,
                  ),
                  MetricCard(
                    label: 'Net P&L',
                    value: '\$${netPnl.toStringAsFixed(2)}',
                    icon: Icons.attach_money,
                    valueColor: netPnl >= 0 ? AppTheme.accentPrimary : AppTheme.accentDanger,
                  ),
                ],
              ),
              const SizedBox(height: 16),

              // Max Drawdown
              MetricCard(
                label: 'Max Drawdown',
                value: '${maxDrawdown.toStringAsFixed(1)}%',
                subtitle: 'Peak to trough decline',
                icon: Icons.trending_down,
                valueColor: maxDrawdown <= 5 ? AppTheme.accentPrimary : AppTheme.accentDanger,
              ),
            ],
          ),
        );
      },
    );
  }
}

class _EquityChart extends StatelessWidget {
  final ApiClient api;

  const _EquityChart({required this.api});

  @override
  Widget build(BuildContext context) {
    return FutureBuilder(
      future: api.getList('/api/equity?days=90'),
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }

        final data = snapshot.data ?? [];
        if (data.isEmpty) {
          return const EmptyView(message: 'No equity data available');
        }

        final spots = <FlSpot>[];
        double maxEquity = 0;
        for (int i = 0; i < data.length; i++) {
          final equity = (data[i]['equity'] as num?)?.toDouble() ?? 0;
          spots.add(FlSpot(i.toDouble(), equity));
          if (equity > maxEquity) maxEquity = equity;
        }

        return LineChart(
          LineChartData(
            gridData: FlGridData(
              show: true,
              drawVerticalLine: false,
              horizontalInterval: maxEquity / 4,
              getDrawingHorizontalLine: (value) => FlLine(
                color: AppTheme.borderPrimary,
                strokeWidth: 1,
              ),
            ),
            titlesData: FlTitlesData(
              show: true,
              rightTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
              topTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
              bottomTitles: AxisTitles(
                sideTitles: SideTitles(
                  showTitles: true,
                  reservedSize: 22,
                  getTitlesWidget: (value, meta) => Text(
                    '${value.toInt()}',
                    style: GoogleFonts.inter(
                      fontSize: 10,
                      color: AppTheme.textMuted,
                    ),
                  ),
                ),
              ),
              leftTitles: AxisTitles(
                sideTitles: SideTitles(
                  showTitles: true,
                  reservedSize: 50,
                  getTitlesWidget: (value, meta) => Text(
                    '\$${value.toInt()}',
                    style: GoogleFonts.jetBrainsMono(
                      fontSize: 10,
                      color: AppTheme.textMuted,
                    ),
                  ),
                ),
              ),
            ),
            borderData: FlBorderData(show: false),
            lineBarsData: [
              LineChartBarData(
                spots: spots,
                isCurved: true,
                gradient: LinearGradient(
                  colors: [AppTheme.accentPrimary, AppTheme.accentSecondary],
                ),
                barWidth: 2,
                isStrokeCapRound: true,
                dotData: const FlDotData(show: false),
                belowBarData: BarAreaData(
                  show: true,
                  gradient: LinearGradient(
                    colors: [
                      AppTheme.accentPrimary.withOpacity(0.2),
                      AppTheme.accentSecondary.withOpacity(0.05),
                    ],
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _TradesTab extends ConsumerWidget {
  const _TradesTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);

    return FutureBuilder(
      future: api.getList('/api/trades'),
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const LoadingIndicator();
        }

        if (snapshot.hasError) {
          return ErrorView(message: 'Failed to load trades');
        }

        final data = snapshot.data ?? [];
        if (data.isEmpty) {
          return const EmptyView(message: 'No trades yet');
        }

        return ListView.builder(
          padding: const EdgeInsets.all(16),
          itemCount: data.length,
          itemBuilder: (context, index) {
            final trade = data[index];
            return Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: TradeListItem(
                symbol: trade['symbol'] ?? 'XAUUSD',
                direction: trade['direction'] ?? 'BUY',
                entryPrice: (trade['entry_price'] as num?)?.toDouble() ?? 0,
                exitPrice: (trade['exit_price'] as num?)?.toDouble(),
                pnlPips: (trade['pnl_pips'] as num?)?.toDouble() ?? 0,
                pnlUsd: (trade['pnl_usd'] as num?)?.toDouble() ?? 0,
                strategy: trade['strategy'] ?? 'V11',
                timestamp: trade['entry_time'] ?? '',
                status: trade['status'] ?? 'open',
              ),
            );
          },
        );
      },
    );
  }
}

class _SettingsTab extends ConsumerWidget {
  const _SettingsTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final user = ref.watch(authProvider).value;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        AppCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Account',
                style: GoogleFonts.inter(
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                  color: AppTheme.textPrimary,
                ),
              ),
              const SizedBox(height: 12),
              _SettingsItem(
                label: 'Email',
                value: user?.email ?? '',
                icon: Icons.email_outlined,
              ),
              const Divider(color: AppTheme.borderPrimary),
              _SettingsItem(
                label: 'Role',
                value: (user?.role ?? 'client').toUpperCase(),
                icon: Icons.badge_outlined,
              ),
              const Divider(color: AppTheme.borderPrimary),
              _SettingsItem(
                label: 'Client ID',
                value: '${user?.clientId ?? 'N/A'}',
                icon: Icons.business_outlined,
              ),
            ],
          ),
        ),
        const SizedBox(height: 16),
        AppCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Security',
                style: GoogleFonts.inter(
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                  color: AppTheme.textPrimary,
                ),
              ),
              const SizedBox(height: 12),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.fingerprint, color: AppTheme.accentPrimary),
                title: Text(
                  'Biometric Unlock',
                  style: GoogleFonts.inter(color: AppTheme.textPrimary),
                ),
                subtitle: Text(
                  'Use Face ID or fingerprint',
                  style: GoogleFonts.inter(color: AppTheme.textMuted, fontSize: 12),
                ),
                trailing: Switch(
                  value: true,
                  onChanged: (v) {},
                  activeColor: AppTheme.accentPrimary,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _SettingsItem extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;

  const _SettingsItem({
    required this.label,
    required this.value,
    required this.icon,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        children: [
          Icon(icon, size: 18, color: AppTheme.textSecondary),
          const SizedBox(width: 12),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                label,
                style: GoogleFonts.inter(
                  fontSize: 11,
                  color: AppTheme.textMuted,
                ),
              ),
              Text(
                value,
                style: GoogleFonts.inter(
                  fontSize: 14,
                  color: AppTheme.textPrimary,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
