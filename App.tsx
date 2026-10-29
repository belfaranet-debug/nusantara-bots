// Titik masuk aplikasi — React Navigation (native-stack).
// Home (daftar chat) adalah layar awal. Header bawaan navigation
// DIMATIKAN di semua layar (headerShown: false) karena tiap layar
// menggambar top bar sendiri. CreateSheet tampil sebagai modal
// transparan di atas layar aktif.

import React, { useEffect } from 'react';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { tryImportByoConfig } from './src/api/byoImport';
import { loadAvatarOverrides } from './src/api/avatar';
import { HomeScreen } from './src/screens/HomeScreen';
import { ChatScreen } from './src/screens/ChatScreen';
// Office (Kantor 3D) DIHAPUS dari repo (Sam 28 Okt — "belum maksimal").
import { ComputerScreen } from './src/screens/ComputerScreen';
import { TeamComputerScreen } from './src/screens/TeamComputerScreen';
import { McpScreen } from './src/screens/McpScreen';
import { SettingsScreen } from './src/screens/SettingsScreen';
import { RoomsScreen } from './src/screens/RoomsScreen';
// SPIKE 0.11.0 — HAPUS SEBELUM RILIS (BUILD-0.11.0-DESIGN.md §7.4).
import { OpenUiSpikeScreen } from './src/screens/OpenUiSpikeScreen';
import { CreateSheet } from './src/components/CreateSheet';
import type { RootStackParamList } from './src/navigation';

const Stack = createNativeStackNavigator<RootStackParamList>();

export default function App() {
  useEffect(() => {
    // One-time BYO import: reads byo-import.json from the app
    // document directory when present, saves it to SecureStore and
    // deletes the file. Absent/invalid files are ignored silently.
    tryImportByoConfig().catch(() => undefined);
    // Override avatar per bot (0.9.8): muat dari AsyncStorage ke
    // cache api/blob supaya GhostBlob/Ghost3D langsung memakai
    // look yang dipilih Sam (tanpa override = hash deterministik).
    loadAvatarOverrides().catch(() => undefined);
  }, []);

  return (
    <NavigationContainer>
      <Stack.Navigator
        initialRouteName="Home"
        screenOptions={{ headerShown: false, contentStyle: { backgroundColor: '#000000' } }}
      >
        <Stack.Screen name="Home" component={HomeScreen} />
        <Stack.Screen name="Chat" component={ChatScreen} />
        <Stack.Screen name="Computer" component={ComputerScreen} />
        <Stack.Screen name="TeamComputer" component={TeamComputerScreen} />
        <Stack.Screen name="Mcp" component={McpScreen} />
        <Stack.Screen name="Settings" component={SettingsScreen} />
        <Stack.Screen name="Rooms" component={RoomsScreen} />
        {/* SPIKE 0.11.0 — HAPUS SEBELUM RILIS. */}
        <Stack.Screen name="OpenUiSpike" component={OpenUiSpikeScreen} />
        <Stack.Screen
          name="CreateSheet"
          component={CreateSheet}
          options={{ presentation: 'transparentModal', animation: 'fade' }}
        />
      </Stack.Navigator>
    </NavigationContainer>
  );
}
