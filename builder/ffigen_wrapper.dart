import 'dart:convert';
import 'dart:io';
import 'package:yaml/yaml.dart';

dynamic plain(dynamic value) {
  if (value is Map) {
    return value.map((key, item) => MapEntry(key.toString(), plain(item)));
  }
  if (value is List) return value.map(plain).toList();
  return value;
}

Future<void> main(List<String> arguments) async {
  final args = List<String>.of(arguments);
  final executable = args.removeAt(0);
  final index = args.indexOf('--config');
  if (index < 0 || index + 1 >= args.length) {
    throw ArgumentError('ffigen --config is required');
  }
  final config = plain(loadYaml(File(args[index + 1]).readAsStringSync())) as Map;
  config['compiler-opts-automatic'] = {
    'macos': {'include-c-standard-library': false}
  };
  final directory = Directory(Platform.environment['ECZ_BRIDGE_DIAGNOSTICS']!);
  directory.createSync(recursive: true);
  final configFile = File('${directory.path}/ffigen.json');
  configFile.writeAsStringSync(jsonEncode(config));
  final headers = config['headers']['entry-points'] as List;
  for (var i = 0; i < headers.length; i++) {
    File(headers[i] as String).copySync('${directory.path}/header-$i.h');
  }
  args[index + 1] = configFile.path;
  final result = await Process.run(executable, args);
  final output = '${result.stdout}${result.stderr}';
  stdout.write(result.stdout);
  stderr.write(result.stderr);
  exitCode = result.exitCode;
  if (output.contains('[SEVERE]') || output.contains('[ERROR]')) {
    exitCode = 1;
  }
}
