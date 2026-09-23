import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:google_fonts/google_fonts.dart';

import '../../services/api_client.dart';
import '../../shared/theme.dart';
import '../../shared/widgets.dart';

class AdminHomeScreen extends ConsumerStatefulWidget {
  const AdminHomeScreen({super.key});

  @override
  ConsumerState<AdminHomeScreen> createState() => _AdminHomeScreenState();
}

class _AdminHomeScreenState extends ConsumerState<AdminHomeScreen> {
  int _selectedIndex = 0;

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.of(context).size;
    final isWide = size.width > 800;

    return Scaffold(
      backgroundColor: AppTheme.bgPrimary,
      appBar: AppBar(
        title: const Text('Admin Dashboard'),
        automaticallyImplyLeading: false,
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: () => setState(() {}),
          ),
          IconButton(
            icon: const Icon(Icons.logout),
            onPressed: () => ref.read(authProvider.notifier).logout(),
          ),
        ],
      ),
      body: Row(
        children: [
          if (isWide)
            NavigationRail(
              selectedIndex: _selectedIndex,
              onDestinationSelected: (i) => setState(() => _selectedIndex = i),
              backgroundColor: AppTheme.bgSecondary,
              selectedIconTheme: const IconThemeData(color: AppTheme.accentPrimary),
              unselectedIconTheme: const IconThemeData(color: AppTheme.textMuted),
              labelType: NavigationRailLabelType.all,
              destinations: const [
                NavigationRailDestination(
                  icon: Icon(Icons.dashboard_outlined),
                  label: Text('Overview'),
                ),
                NavigationRailDestination(
                  icon: Icon(Icons.people_outlined),
                  label: Text('Clients'),
                ),
                NavigationRailDestination(
                  icon: Icon(Icons.shield_outlined),
                  label: Text('Risk'),
                ),
                NavigationRailDestination(
                  icon: Icon(Icons.settings_outlined),
                  label: Text('Settings'),
                ),
              ],
            ),
          Expanded(
            child: IndexedStack(
              index: _selectedIndex,
              children: const [
                _OverviewTab(),
                _ClientsTab(),
                _RiskTab(),
                _AdminSettingsTab(),
              ],
            ),
          ),
        ],
      ),
      bottomNavigationBar: isWide
          ? null
          : BottomNavigationBar(
              currentIndex: _selectedIndex,
              onTap: (i) => setState(() => _selectedIndex = i),
              items: const [
                BottomNavigationBarItem(
                  icon: Icon(Icons.dashboard_outlined),
                  label: 'Overview',
                ),
                BottomNavigationBarItem(
                  icon: Icon(Icons.people_outlined),
                  label: 'Clients',
                ),
                BottomNavigationBarItem(
                  icon: Icon(Icons.shield_outlined),
                  label: 'Risk',
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

class _OverviewTab extends ConsumerWidget {
  const _OverviewTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);

    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'System Overview',
            style: GoogleFonts.inter(
              fontSize: 20,
              fontWeight: FontWeight.w700,
              color: AppTheme.textPrimary,
            ),
          ),
          const SizedBox(height: 16),

          // Summary cards
          GridView.count(
            crossAxisCount: MediaQuery.of(context).size.width > 1000 ? 4 : 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 12,
            crossAxisSpacing: 12,
            childAspectRatio: 1.8,
            children: const [
              MetricCard(
                label: 'Total Clients',
                value: '2',
                icon: Icons.people,
              ),
              MetricCard(
                label: 'Active Trades',
                value: '1',
                icon: Icons.swap_horiz,
              ),
              MetricCard(
                label: 'Total P&L',
                value: '+$102',
                icon: Icons.attach_money,
                valueColor: AppTheme.accentPrimary,
              ),
              MetricCard(
                label: 'Kill Switch',
                value: 'INACTIVE',
                icon: Icons.shield,
                valueColor: AppTheme.accentPrimary,
              ),
            ],
          ),
          const SizedBox(height: 24),

          // Recent trades across all clients
          AppCard(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Recent System Activity',
                  style: GoogleFonts.inter(
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                    color: AppTheme.textPrimary,
                  ),
                ),
                const SizedBox(height: 12),
                FutureBuilder(
                  future: api.getList('/api/trades?limit=10'),
                  builder: (context, snapshot) {
                    if (snapshot.connectionState == ConnectionState.waiting) {
                      return const Padding(
                        padding: EdgeInsets.all(24),
                        child: Center(child: CircularProgressIndicator()),
                      );
                    }

                    final data = snapshot.data ?? [];
                    if (data.isEmpty) {
                      return const EmptyView(message: 'No recent activity');
                    }

                    return Column(
                      children: data.take(5).map<Widget>((trade) {
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
                      }).toList(),
                    );
                  },
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ClientsTab extends ConsumerWidget {
  const _ClientsTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);

    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Clients',
            style: GoogleFonts.inter(
              fontSize: 20,
              fontWeight: FontWeight.w700,
              color: AppTheme.textPrimary,
            ),
          ),
          const SizedBox(height: 16),
          FutureBuilder(
            future: api.getList('/api/admin/clients'),
            builder: (context, snapshot) {
              if (snapshot.connectionState == ConnectionState.waiting) {
                return const LoadingIndicator();
              }

              if (snapshot.hasError) {
                return ErrorView(message: 'Failed to load clients');
              }

              final data = snapshot.data ?? [];
              if (data.isEmpty) {
                return const EmptyView(message: 'No clients registered');
              }

              return ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                itemCount: data.length,
                itemBuilder: (context, index) {
                  final client = data[index];
                  return Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: AppCard(
                      child: ListTile(
                        contentPadding: const EdgeInsets.all(8),
                        leading: CircleAvatar(
                          backgroundColor: AppTheme.accentPrimary.withOpacity(0.1),
                          child: Text(
                            (client['name'] ?? 'C')[0],
                            style: const TextStyle(color: AppTheme.accentPrimary),
                          ),
                        ),
                        title: Text(
                          client['name'] ?? 'Unknown',
                          style: GoogleFonts.inter(
                            fontWeight: FontWeight.w600,
                            color: AppTheme.textPrimary,
                          ),
                        ),
                        subtitle: Text(
                          '${client['total_trades']} trades · ${(client['win_rate'] * 100).toStringAsFixed(1)}% win',
                          style: GoogleFonts.inter(
                            color: AppTheme.textSecondary,
                            fontSize: 12,
                          ),
                        ),
                        trailing: Text(
                          '\$${(client['net_pnl_usd'] as double).toStringAsFixed(2)}',
                          style: GoogleFonts.jetBrainsMono(
                            fontWeight: FontWeight.w600,
                            color: (client['net_pnl_usd'] as double) >= 0
                                ? AppTheme.accentPrimary
                                : AppTheme.accentDanger,
                          ),
                        ),
                        onTap: () {
                          Navigator.push(
                            context,
                            MaterialPageRoute(
                              builder: (_) => ClientDetailScreen(
                                clientId: client['id'],
                                clientName: client['name'],
                              ),
                            ),
                          );
                        },
                      ),
                    ),
                  );
                },
              );
            },
          ),
        ],
      ),
    );
  }
}

class _RiskTab extends ConsumerWidget {
  const _RiskTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final api = ref.watch(apiProvider);

    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Risk Monitor',
            style: GoogleFonts.inter(
              fontSize: 20,
              fontWeight: FontWeight.w700,
              color: AppTheme.textPrimary,
            ),
          ),
          const SizedBox(height: 16),
          FutureBuilder(
            future: api.getList('/api/admin/risk_state'),
            builder: (context, snapshot) {
              if (snapshot.connectionState == ConnectionState.waiting) {
                return const LoadingIndicator();
              }

              if (snapshot.hasError) {
                return ErrorView(message: 'Failed to load risk data');
              }

              final data = snapshot.data ?? [];
              if (data.isEmpty) {
                return const EmptyView(message: 'No risk data available');
              }

              return ListView.builder(
                shrinkWrap: true,
                physics: const NeverScrollableScrollPhysics(),
                itemCount: data.length,
                itemBuilder: (context, index) {
                  final risk = data[index];
                  final killActive = risk['kill_switch_active'] == 1;

                  return Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: AppCard(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            mainAxisAlignment: MainAxisAlignment.spaceBetween,
                            children: [
                              Text(
                                'Client #${risk['client_id']}',
                                style: GoogleFonts.inter(
                                  fontWeight: FontWeight.w600,
                                  color: AppTheme.textPrimary,
                                ),
                              ),
                              StatusBadge(
                                status: killActive ? 'BLOCKED' : 'ACTIVE',
                              ),
                            ],
                          ),
                          const SizedBox(height: 12),
                          Row(
                            children: [
                              _RiskMetric(
                                label: 'Daily P&L',
                                value: '\$${(risk['daily_pnl_usd'] as double).toStringAsFixed(2)}',
                                isNegative: (risk['daily_pnl_usd'] as double) < 0,
                              ),
                              _RiskMetric(
                                label: 'Weekly P&L',
                                value: '\$${(risk['weekly_pnl_usd'] as double).toStringAsFixed(2)}',
                                isNegative: (risk['weekly_pnl_usd'] as double) < 0,
                              ),
                              _RiskMetric(
                                label: 'Consec. Losses',
                                value: '${risk['consecutive_losses']}',
                                isNegative: (risk['consecutive_losses'] as int) >= 2,
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                  );
                },
              );
            },
          ),
        ],
      ),
    );
  }
}

class _RiskMetric extends StatelessWidget {
  final String label;
  final String value;
  final bool isNegative;

  const _RiskMetric({
    required this.label,
    required this.value,
    required this.isNegative,
  });

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: GoogleFonts.inter(
              fontSize: 11,
              color: AppTheme.textMuted,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            style: GoogleFonts.jetBrainsMono(
              fontSize: 14,
              fontWeight: FontWeight.w600,
              color: isNegative ? AppTheme.accentDanger : AppTheme.textPrimary,
            ),
          ),
        ],
      ),
    );
  }
}

class _AdminSettingsTab extends ConsumerWidget {
  const _AdminSettingsTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        AppCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'System Settings',
                style: GoogleFonts.inter(
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                  color: AppTheme.textPrimary,
                ),
              ),
              const SizedBox(height: 16),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(
                  'API Endpoint',
                  style: GoogleFonts.inter(color: AppTheme.textPrimary),
                ),
                subtitle: Text(
                  'http://localhost:8000',
                  style: GoogleFonts.jetBrainsMono(
                    color: AppTheme.textMuted,
                    fontSize: 12,
                  ),
                ),
                trailing: const Icon(Icons.edit, color: AppTheme.textSecondary),
              ),
              const Divider(color: AppTheme.borderPrimary),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(
                  'Session Timeout',
                  style: GoogleFonts.inter(color: AppTheme.textPrimary),
                ),
                subtitle: Text(
                  '30 minutes',
                  style: GoogleFonts.inter(color: AppTheme.textMuted, fontSize: 12),
                ),
                trailing: const Icon(Icons.edit, color: AppTheme.textSecondary),
              ),
              const Divider(color: AppTheme.borderPrimary),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(
                  'Kill All Trading',
                  style: GoogleFonts.inter(color: AppTheme.accentDanger),
                ),
                subtitle: Text(
                  'Emergency stop for all clients',
                  style: GoogleFonts.inter(color: AppTheme.textMuted, fontSize: 12),
                ),
                trailing: TextButton(
                  onPressed: () {
                    // Flag: this needs explicit user confirmation before implementation
                    showDialog(
                      context: context,
                      builder: (_) => AlertDialog(
                        backgroundColor: AppTheme.bgCard,
                        title: const Text('Confirm Kill Switch'),
                        content: const Text('This will halt all trading. Continue?'),
                        actions: [
                          TextButton(
                            onPressed: () => Navigator.pop(context),
                            child: const Text('Cancel'),
                          ),
                          TextButton(
                            onPressed: () {
                              Navigator.pop(context);
                              // TODO: Implement kill switch API call
                            },
                            child: const Text('CONFIRM', style: TextStyle(color: AppTheme.accentDanger)),
                          ),
                        ],
                      ),
                    );
                  },
                  child: const Text('TRIP'),
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class ClientDetailScreen extends StatelessWidget {
  final int clientId;
  final String clientName;

  const ClientDetailScreen({
    super.key,
    required this.clientId,
    required this.clientName,
  });

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.bgPrimary,
      appBar: AppBar(
        title: Text(clientName),
      ),
      body: FutureBuilder(
        future: ref.read(apiProvider).getList('/api/admin/clients/$clientId/trades'),
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingIndicator();
          }

          if (snapshot.hasError) {
            return ErrorView(message: 'Failed to load client data');
          }

          final data = snapshot.data ?? [];
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
      ),
    );
  }
}
