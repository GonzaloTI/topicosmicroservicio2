from __future__ import annotations
from dataclasses import dataclass
from threading import RLock, Condition
from typing import List, Optional, Tuple
import time
import redis

from manager.cola2 import Cola2
from manager.tarea import Metodo, Prioridad
from manager.task_manager import WorkerManager

@dataclass
class RedisParams:
    host: str = "localhost"
    port: int = 6379
    password: Optional[str] = None
    db: int = 0

@dataclass
class ColaSlot:
    cola: Cola2
    ocupada: bool = False
    worker_manager: Optional[WorkerManager] = None   # define un worquermanager para cada cola

class ColaManager:
    """
    Crea y administra internamente múltiples colas Cola2.
    - create_queue(nombre)
    - create_many(n, prefix)
    - delete_queue(nombre, drop_data=False, drop_status=False)
    - list_queues()
    - agregar_tarea(metodo, prioridad, payload, timeout=2.0, strategy='first_free')
    """
    def __init__(self, params: RedisParams,dborm,bzpop_timeout: int = 1):
        self._p = params
        self._slots: List[ColaSlot] = []
        self._lock = RLock()
        self._cv = Condition(self._lock)
        self._r = redis.Redis(
            host=params.host, port=params.port,
            password=params.password, db=params.db,
            decode_responses=True
        )
        self._rr_idx = 0  # <- puntero de round-robin
        self.bzpop_timeout=bzpop_timeout
        self.dborm = dborm
        
        self.balanceo_activo: bool = False
        self.colabalanceo: dict[str, dict] = {}  # nombre -> {"objetivo": int, "restante": int}
        self._bal_order: list[str] = []          # orden estable de colas en balanceo
        self._bal_idx: int = 0  

    # ---------- creación / eliminación ----------
    def create_queue(self, nombre: str,num_workers: int = 1) -> Cola2:
        if not nombre:
            raise ValueError("Nombre de cola inválido")
        with self._lock:
            for s in self._slots:
                if s.cola.nombre == nombre:
                    return s.cola
            
            objetivo = self._max_carga_total_old() # calcular el objetivo del balanceo antes de crearlo
                 
            cola = Cola2(
                redis_host=self._p.host,
                redis_port=self._p.port,
                redis_password=self._p.password,
                redis_db=self._p.db,
                nombre=nombre,
            )
            wm = None
            if num_workers > 0 and self.dborm is not None:
                wm = WorkerManager(
                    cola2=cola,
                    dborm=self.dborm,
                    num_workers=num_workers,
                    bzpop_timeout=self.bzpop_timeout
                )
                wm.start()

            self._slots.append(ColaSlot(cola=cola, worker_manager=wm))
            self._cv.notify_all()
            
            if objetivo >= 5:
            # colabalanceo_add debe existir y setear balanceo_activo
                self.colabalanceo_add(nombre, objetivo=objetivo)
            
            return cola
        
    def _max_carga_total_old(self) -> int:
        """
        Máximo de (pendientes + realizadas) entre colas 'viejas'.
        Excluye colas que ya están en colabalanceo (opcionales) para no sesgar el target.
        """
        bases = [s for s in self._slots if s.cola.nombre not in self.colabalanceo]
        if not bases:
            return 0
        max_carga = 0
        for s in bases:
            try:
                p = s.cola.count_pendientes()
                r = s.cola.count_realizadas()
            except Exception:
                p, r = 0, 0
            max_carga = max(max_carga, p + r)
        return max_carga
    
    def create_many(self, n: int, prefix: str = "cola", num_workers: int = 1) -> List[str]:
        names = []
        with self._lock:
            for i in range(1, max(1, n) + 1):
                name = f"{prefix}_{i}"
                if not any(s.cola.nombre == name for s in self._slots):
                    cola = Cola2(
                        redis_host=self._p.host,
                        redis_port=self._p.port,
                        redis_password=self._p.password,
                        redis_db=self._p.db,
                        nombre=name,
                    )
                    wm = None
                    if num_workers > 0 and self.dborm is not None:
                        wm = WorkerManager(
                            cola2=cola,
                            dborm=self.dborm,
                            num_workers=num_workers,
                            bzpop_timeout=self.bzpop_timeout
                        )
                        wm.start()

                    self._slots.append(ColaSlot(cola=cola, worker_manager=wm))
                    names.append(name)
                
            if names:
                self._cv.notify_all()
        return names

    def delete_queue(self, nombre: str, drop_data: bool = False, drop_status: bool = False) -> dict:
        """
        Elimina una cola de forma segura:
        1) La retira del manager para que no sea elegible.
        2) Detiene todos sus workers.
        3) Borra keys de Redis opcionalmente.
        Devuelve un dict con detalles de lo realizado.
        """
        slot = None

        # 1) localizar y retirar el slot bajo lock (para que nadie la use)
        with self._lock:
            idx = next((i for i, s in enumerate(self._slots) if s.cola.nombre == nombre), None)
            if idx is None:
                return {"removed": False, "reason": "queue-not-found"}

            # si está marcada ocupada por una asignación en curso, no borramos
            if self._slots[idx].ocupada:
                return {"removed": False, "reason": "queue-busy"}

            # sacar el slot del manager para que no pueda ser elegido
            slot = self._slots.pop(idx)
            self._cv.notify_all()

        stopped = False
        # 2) detener los workers FUERA del lock
        if slot.worker_manager is not None:
            slot.worker_manager.stop_all()
            stopped = True

        # 3) borrar keys en Redis (opcional)
        deleted_keys = []
        if drop_data:
            deleted_keys.append(nombre)  # zset/list principal de tu Cola2 (ajusta si usas otro nombre)
        if drop_status:
            deleted_keys.append(f"{nombre}:status")

        if deleted_keys:
            # ignorar si no existen, Redis delete devuelve 0/1 sin lanzar error
            try:
                self._r.delete(*deleted_keys)
            except Exception:
                pass

        return {
            "removed": True,
            "queue": nombre,
            "workers_stopped": stopped,
            "deleted_keys": deleted_keys
        }


    def list_queues(self) -> List[dict]:
        resultado = []
        with self._lock:
            for s in self._slots:
                wm = s.worker_manager
                if wm and getattr(wm, "workers", None):
                    num_workers = len(wm.workers)

                    # Determinar estado global:
                    estados = []
                    for w in wm.workers:
                        if getattr(w, "_stop_event", None) and w._stop_event.is_set():
                            estados.append("stopped")
                        elif not w.is_alive():
                            estados.append("dead")
                        elif getattr(w, "_run_event", None) and not w._run_event.is_set():
                            estados.append("paused")
                        else:
                            estados.append("running")
                    # Si todos iguales → ese estado, si no → mixed
                    estado_global = estados[0] if estados and all(st == estados[0] for st in estados) else "mixed"

                else:
                    num_workers = 0
                    estado_global = "no-workers"

                resultado.append({
                    "cola": s.cola.nombre,
                    "workers_total": num_workers,
                    "estado": estado_global
                })
        return resultado

    # ---------- selección de cola ----------
    def _pick_first_free(self) -> Optional[ColaSlot]:
        for s in self._slots:
            if not s.ocupada:
                return s
        return None

    def _pick_least_backlog(self) -> Optional[ColaSlot]:
        libres = [s for s in self._slots if not s.ocupada]
        if not libres:
            return None
        # menor número de pendientes en el ZSET
        return min(libres, key=lambda s: s.cola.count_pendientes())

    # ---------- API principal: asignar y encolar ----------
    def agregar_tarea(
        self,
        metodo: Metodo,
        prioridad: Prioridad,
        payload=None,
        timeout: float = 2.0,
        strategy: str = "first_free",  # "first_free" | "least_backlog"
    ) -> Tuple[str, str]:
        """
        Elige una cola 'libre', la marca ocupada, encola la tarea y libera.
        Devuelve (nombre_cola, id_tarea).
        Lanza RuntimeError si no hay colas o si se agota el timeout.
        """
        chooser = self._pick_first_free if strategy == "first_free" else self._pick_least_backlog
        end = time.time() + (timeout if timeout is not None else 0)

        with self._lock:
            while True:
                if not self._slots:
                    raise RuntimeError("No hay colas creadas en el manager")
                slot = chooser()
                if slot:
                    slot.ocupada = True
                    cola = slot.cola
                    break
                if timeout is None:
                    self._cv.wait(timeout=0.25)
                else:
                    rem = end - time.time()
                    if rem <= 0:
                        raise RuntimeError("No hay colas libres (timeout)")
                    self._cv.wait(timeout=min(0.25, rem))

        # fuera del lock: realizar el encolado real
        try:
            tarea_id = cola.agregar(metodo=metodo, prioridad=prioridad, payload=payload)
            return cola.nombre, tarea_id
        finally:
            with self._lock:
                slot.ocupada = False
                self._cv.notify()
    # def _pick_round_robin(self) -> Optional[ColaSlot]:
    #     """Devuelve el siguiente slot NO ocupado en orden circular."""
    #     n = len(self._slots)
    #     if n == 0:
    #         return None
    #     start = self._rr_idx
    #     for k in range(n):
    #         idx = (start + k) % n
    #         s = self._slots[idx]
    #         if not s.ocupada:
    #             # avanza el puntero para la próxima asignación
    #             self._rr_idx = (idx + 1) % n
    #             return s
    #     return None
    # def agregar_tarea_Round_Robin(
    #     self,
    #     metodo: Metodo,
    #     prioridad: Prioridad,
    #     payload=None,
    #     timeout: float = 5.0
    #     ) -> Tuple[str, str]:
    #     chooser = self._pick_round_robin
    #     end = time.time() + (timeout if timeout is not None else 0)

    #     with self._lock:
    #         while True:
    #             if not self._slots:
    #                 raise RuntimeError("No hay colas creadas en el manager")
    #             slot = chooser()
    #             if slot:
    #                 slot.ocupada = True
    #                 cola = slot.cola
    #                 break
    #             if timeout is None:
    #                 self._cv.wait(timeout=0.25)
    #             else:
    #                 rem = end - time.time()
    #                 if rem <= 0:
    #                     raise RuntimeError("No hay colas libres (timeout)")
    #                 self._cv.wait(timeout=min(0.25, rem))

    #     try:
    #         tarea_id = cola.agregar(metodo=metodo, prioridad=prioridad, payload=payload)
    #         return cola.nombre, tarea_id
    #     finally:
    #         with self._lock:
    #             slot.ocupada = False
    #             self._cv.notify()
# --- reemplaza estos dos métodos en tu ColaManager ---

    def _pick_round_robin(self) -> Optional[ColaSlot]:
        """
        Devuelve el siguiente slot en orden circular, sin mirar 'ocupada'.
        La elección se hace bajo lock por el llamador.
        """
        n = len(self._slots)
        if n == 0:
            return None
        slot = self._slots[self._rr_idx % n]
        self._rr_idx = (self._rr_idx + 1) % n
        return slot

    #def insertar_valanceo(self, metodo: Metodo,prioridad: Prioridad,payload=None,timeout: float | None = None)
    # inserta con valanceo 
    # una funcion que inserta entre los que estan dentro de colabalanceo, , en self.colabalanceo: hay la referencia por id o nombre de la cola y su objetivo ,
    # se va inseratando entre los queesteen dentro de colabalanceo, itera sobre ellos, y les va restando su objeivo una ves que termina se los saca de colabalanceo,
    # sale termina los que estan en colabanaceo y se lossaca de la self.colabalanceo , y queda vacio, y queda vacio, y el round robin ya no entraa aqui 
    def insertar_valanceo(
        self,
        metodo: Metodo,
        prioridad: Prioridad,
        payload=None,
        timeout: float | None = None
        ) -> Tuple[str, str]:
        """
        Inserta una tarea en alguna de las colas listadas en 'colabalanceo',
        repartiendo round-robin entre ellas y decrementando su 'restante'.
        Si ya no hay balanceo, cae al RR normal.
        """
        # Asegurar que existan colas
        print("insertando en colabalanceo")
        
        if timeout is not None:
            end = time.time() + timeout
            with self._lock:
                while not self._slots:
                    rem = end - time.time()
                    if rem <= 0:
                        raise RuntimeError("No hay colas creadas en el manager")
                    self._cv.wait(timeout=min(0.25, rem))
        else:
            with self._lock:
                while not self._slots:
                    self._cv.wait(timeout=0.25)

        # Elegir destino del balanceo bajo lock
        with self._lock:
            slot = self._colabalanceo_pick_slot_locked()
            if slot is None:
                # Sin balanceo válido → RR normal
                self.balanceo_activo = False
                slot = self._pick_round_robin()
            cola = slot.cola
            nombre = cola.nombre

        # Encolar fuera del lock
        tarea_id = cola.agregar(metodo=metodo, prioridad=prioridad, payload=payload)

        # Post-actualización del balanceo
        with self._lock:
            if nombre in self.colabalanceo:
                self.colabalanceo[nombre]["restante"] -= 1
                if self.colabalanceo[nombre]["restante"] <= 0:
                    # terminó su objetivo
                    self._colabalanceo_prune()  # esto también puede apagar balanceo si queda vacío

        return nombre, tarea_id

    def colabalanceo_add(self, nombre: str, objetivo: int) -> None:
        with self._lock:
            objetivo = max(0, int(objetivo))
            if nombre not in self.colabalanceo:
                self.colabalanceo[nombre] = {"objetivo": objetivo, "restante": objetivo}
                self._bal_order.append(nombre)
            else:
                self.colabalanceo[nombre]["objetivo"] = objetivo
                self.colabalanceo[nombre]["restante"] = objetivo
                if nombre not in self._bal_order:
                    self._bal_order.append(nombre)
            self.balanceo_activo = True
            if self._bal_order:
                self._bal_idx %= max(1, len(self._bal_order))

    def _colabalanceo_prune(self) -> None:
        """Limpia colas terminadas o inexistentes, Apaga balanceo si ya no queda ninguna."""
        changed = False
        for nombre in list(self._bal_order):
            meta = self.colabalanceo.get(nombre)
            slot = self._get_slot(nombre)
            if (meta is None) or (slot is None) or (meta.get("restante", 0) <= 0):
                # quitar del diccionario y del orden
                self.colabalanceo.pop(nombre, None)
                i = self._bal_order.index(nombre)
                self._bal_order.pop(i)
                if i <= self._bal_idx and self._bal_idx > 0:
                    self._bal_idx -= 1
                changed = True
        if changed and self._bal_order:
            self._bal_idx %= max(1, len(self._bal_order))
        if not self._bal_order:
            # no queda nadie en balanceo
            self.colabalanceo.clear()
            self._bal_idx = 0
            self.balanceo_activo = False

    def _colabalanceo_pick_slot_locked(self) -> Optional[ColaSlot]:
        """
        Selecciona la siguiente cola del grupo de balanceo (RR interno),
        asumiendo que ya estamos dentro de self._lock.
        """
        self._colabalanceo_prune()
        if not self.balanceo_activo or not self._bal_order:
            return None

        n = len(self._bal_order)
        start = self._bal_idx % n
        for k in range(n):
            idx = (start + k) % n
            nombre = self._bal_order[idx]
            meta = self.colabalanceo.get(nombre)
            slot = self._get_slot(nombre)
            if meta and slot and meta.get("restante", 0) > 0:
                # Avanza puntero para la próxima selección
                self._bal_idx = (idx + 1) % n
                return slot

        # Si ninguna tenía restante, podar y terminar
        self._colabalanceo_prune()
        return None


    def agregar_tarea_Round_Robin(
        self,
        metodo: Metodo,
        prioridad: Prioridad,
        payload=None,
        timeout: float | None = None
        ) -> Tuple[str, str]:
        """
        Elige una cola por round-robin bajo lock y encola fuera del lock.
        No bloquea por 'ocupada' porque encolar en Redis es seguro en paralelo.
        Si no existen colas aún, puede esperar hasta 'timeout' a que se creen.
        """
        with self._lock:
            hay_balanceo = self.balanceo_activo and bool(self.colabalanceo)

        if hay_balanceo:
            return self.insertar_valanceo(metodo, prioridad, payload=payload, timeout=timeout)
        
        if timeout is not None:
            end = time.time() + timeout
            with self._lock:
                while not self._slots:
                    rem = end - time.time()
                    if rem <= 0:
                        raise RuntimeError("No hay colas creadas en el manager")
                    self._cv.wait(timeout=min(0.25, rem))
                slot = self._pick_round_robin()
                cola = slot.cola
        else:
            # Sin timeout: esperar indefinidamente a que aparezca alguna cola
            with self._lock:
                while not self._slots:
                    self._cv.wait(timeout=0.25)
                slot = self._pick_round_robin()
                cola = slot.cola

        # Fuera del lock: encolar 
        tarea_id = cola.agregar(metodo=metodo, prioridad=prioridad, payload=payload)
        return cola.nombre, tarea_id


    # --- helpers internos ---
    def _get_slot(self, nombre: str) -> Optional[ColaSlot]:
        with self._lock:
            for s in self._slots:
                if s.cola.nombre == nombre:
                    return s
        return None

    # --- controlar workers por cola o global ---
    def pause_workers(self, nombre: Optional[str] = None) -> int:
        """
        Pausa los workers. Si 'nombre' es None, pausa en todas las colas.
        Devuelve WorkerManager afectados.
        """
        afectados = 0
        with self._lock:
            slots = self._slots if nombre is None else [self._get_slot(nombre)]
            for s in slots:
                if s and s.worker_manager:
                    s.worker_manager.pause_all()
                    afectados += 1
        return afectados

    def resume_workers(self, nombre: Optional[str] = None) -> int:
        """
        Reanuda los workers. Si 'nombre' es None, reanuda en todas las colas.
        Devuelve WorkerManager afectados.
        """
        afectados = 0
        with self._lock:
            slots = self._slots if nombre is None else [self._get_slot(nombre)]
            for s in slots:
                if s and s.worker_manager:
                    s.worker_manager.resume_all()
                    afectados += 1
        return afectados

    def stop_workers(self, nombre: Optional[str] = None) -> int:
        """
        Detiene (stop + join) los workers. Si 'nombre' es None, detiene en todas las colas.
        Devuelve WorkerManager afectados.
        """
        afectados = 0
        targets = []
        with self._lock:
            slots = self._slots if nombre is None else [self._get_slot(nombre)]
            for s in slots:
                if s and s.worker_manager:
                    targets.append(s.worker_manager)

        for wm in targets:
            wm.stop_all()
            afectados += 1
        return afectados

    # =======Aumentar y Quitar workers de las colas ==============================

    def add_workers_to_queue(self, nombre: str, n: int) -> dict:
        """
        Añade n workers a la cola 'nombre'. Si no tenía WorkerManager, lo crea.
        """
        if n <= 0:
            return {"ok": False, "reason": "n must be > 0"}

        with self._lock:
            slot = self._get_slot(nombre)
            if not slot:
                return {"ok": False, "reason": "queue-not-found"}

            if slot.worker_manager is None:
                # crear WM nuevo con n workers
                wm = WorkerManager(
                    cola2=slot.cola,
                    dborm=self.dborm,
                    num_workers=0,  # arranca vacío
                    bzpop_timeout=self.bzpop_timeout
                )
                slot.worker_manager = wm
            else:
                wm = slot.worker_manager

        created = wm.add_workers(n)
        return {"ok": True, "queue": nombre, "added": created, "total": wm.count()}

    def remove_workers_from_queue(self, nombre: str, n: int) -> dict:
        """
        Quita n workers de la cola 'nombre' (parando hilos).
        """
        if n <= 0:
            return {"ok": False, "reason": "n must be > 0"}

        with self._lock:
            slot = self._get_slot(nombre)
            if not slot or not slot.worker_manager:
                return {"ok": False, "reason": "no-worker-manager"}

            wm = slot.worker_manager

        removed = wm.remove_workers(n)
        return {"ok": True, "queue": nombre, "removed": removed, "total": wm.count()}

    
