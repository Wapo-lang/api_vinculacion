from odoo import models, fields, api
from datetime import datetime, timedelta
from odoo.exceptions import ValidationError
from odoo.exceptions import UserError
import requests
import logging


_logger = logging.getLogger(__name__)

from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)

from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)

class Libro(models.Model):
    _name = 'biblioteca.libro'
    _description = 'Gestión de Libros'
    _rec_name = 'firstname'
    
    # --- CAMPOS ---
    firstname = fields.Char(string='Nombre Libro', required=True)
    author = fields.Many2one('biblioteca.autor', string='Autor Libro')
    isbn = fields.Char(string='ISBN')
    value = fields.Integer(string='Número de Ejemplares', default=1)
    value2 = fields.Float(compute="_value_pc", store=True, string='Valor Computado')
    description = fields.Text(string='Descripción')
    openlibrary_description = fields.Text(string='Estado de Sincronización')
    ejemplares_disponibles = fields.Integer(string='Ejemplares Disponibles', default=1)

    # --- 1. MÉTODOS DE CÁLCULO (Solución al AttributeError) ---
    
    @api.depends('value')
    def _value_pc(self):
        """Calcula el valor dividido por 100 para el campo value2."""
        for record in self:
            if record.value:
                record.value2 = float(record.value) / 100
            else:
                record.value2 = 0.0

    @api.onchange('value')
    def _onchange_value(self):
        """Actualiza ejemplares disponibles al cambiar el total localmente."""
        self.ejemplares_disponibles = self.value

    # --- 2. MÉTODOS DE CICLO DE VIDA (CRUD) ---

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'value' in vals and 'ejemplares_disponibles' not in vals:
                vals['ejemplares_disponibles'] = vals['value']
        
        records = super(Libro, self).create(vals_list)
        for record in records:
            record._sync_to_django(method='POST')
        return records

    def write(self, vals):
        res = super(Libro, self).write(vals)
        for record in self:
            record._sync_to_django(method='PUT')
        return res

    def unlink(self):
        for record in self:
            _logger.info(f"Eliminando libro {record.firstname} en Django...")
            record._sync_to_django(method='DELETE')
        return super(Libro, self).unlink()

    # --- 3. MOTOR DE SINCRONIZACIÓN API ---

    def _sync_to_django(self, method='POST'):
        BASE_API = "http://127.0.0.1:8000/api/"
        url_libros = f"{BASE_API}libros-api/"
        
        # Sincronización por ISBN para evitar conflictos de ID
        identificador = self.isbn if self.isbn else self.id
        url_especifica = f"{url_libros}{identificador}/" 

        try:
            if method == 'DELETE':
                requests.delete(url_especifica, timeout=5)
                return 

            # Sincronizar Autor primero
            django_author_id = None
            if self.author:
                auth_payload = {'nombre': self.author.firstname, 'apellido': self.author.lastname or '.'}
                auth_res = requests.post(f"{BASE_API}autores-api/", json=auth_payload, timeout=5)
                
                if auth_res.status_code in [200, 201]:
                    django_author_id = auth_res.json().get('id')
                else:
                    search_res = requests.get(f"{BASE_API}autores-api/", timeout=5)
                    if search_res.status_code == 200:
                        match = next((a for a in search_res.json() if a['nombre'] == self.author.firstname), None)
                        django_author_id = match.get('id') if match else None

            libro_payload = {
                'titulo': self.firstname,
                'isbn': str(self.isbn).strip() if self.isbn else '',
                'descripcion': self.description or '',
                'cantidad_total': self.value,
                'ejemplares_disponibles': self.ejemplares_disponibles,
                'autor': django_author_id
            }

            if method == 'POST':
                response = requests.post(url_libros, json=libro_payload, timeout=10)
                if response.status_code == 400 and "isbn" in response.text:
                    return self._sync_to_django(method='PUT')
            
            elif method == 'PUT':
                response = requests.put(url_especifica, json=libro_payload, timeout=10)
                if response.status_code == 404:
                    return self._sync_to_django(method='POST')

            if response.status_code in [200, 201, 204]:
                self.openlibrary_description = f"Sincronización exitosa ({method})."
            else:
                self.openlibrary_description = f"Nota Django: {response.text}"

        except Exception as e:
            _logger.error(f"Error en API {method}: {str(e)}")

    # --- 4. BÚSQUEDA POR ISBN (GET) ---

    @api.onchange('isbn')
    def _onchange_isbn_fetch_data(self):
        if not self.isbn or len(self.isbn) < 5:
            return

        search_isbn = str(self.isbn).strip()
        success_django = False
        
        # 1. INTENTO EN DJANGO
        try:
            django_url = f"http://127.0.0.1:8000/api/libros-api/?isbn={search_isbn}"
            res = requests.get(django_url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and len(data) > 0:
                    book = data[0]
                    self.firstname = book.get('titulo')
                    self.description = book.get('descripcion')
                    
                    # Autor desde Django
                    autor_info = book.get('autor_detalle')
                    if autor_info:
                        self._get_or_create_author(
                            autor_info.get('nombre'), 
                            autor_info.get('apellido')
                        )
                    
                    self.openlibrary_description = "Datos cargados desde Django."
                    success_django = True
        except Exception as e:
            _logger.warning(f"Django no disponible: {e}")

        # 2. INTENTO EN OPEN LIBRARY (Si Django falló o no encontró nada)
        if not success_django:
            ol_url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{search_isbn}&format=json&jscmd=data"
            try:
                response = requests.get(ol_url, timeout=10)
                if response.status_code == 200:
                    res_json = response.json()
                    key = f"ISBN:{search_isbn}"
                    
                    if key in res_json:
                        info = res_json[key]
                        
                        # --- TÍTULO ---
                        self.firstname = info.get('title', 'Sin título')

                        # --- AUTOR (Validación Robusta) ---
                        if info.get('authors'):
                            full_name = info['authors'][0].get('name', '')
                            if full_name:
                                parts = full_name.split(' ', 1)
                                nom = parts[0]
                                ape = parts[1] if len(parts) > 1 else '.'
                                self._get_or_create_author(nom, ape)

                        # --- DESCRIPCIÓN (Validación Robusta) ---
                        # OpenLibrary a veces manda un string, otras un dict {'value': '...'}
                        raw_desc = info.get('description', info.get('notes', 'Sin descripción.'))
                        if isinstance(raw_desc, dict):
                            self.description = raw_desc.get('value', '')
                        else:
                            self.description = str(raw_desc)
                            
                        self.openlibrary_description = "Datos cargados desde Open Library."
                    else:
                        self.openlibrary_description = "ISBN no encontrado en ninguna fuente."
                else:
                    self.openlibrary_description = f"Error en Open Library (Status {response.status_code})"
            
            except Exception as e:
                _logger.error(f"Error procesando Open Library: {e}")
                self.openlibrary_description = f"Error al procesar datos externos: {str(e)}"
    
    def _get_or_create_author(self, nombre, apellido):
        """
        Busca un autor por nombre y apellido. 
        Si no existe, lo crea. Luego lo asigna al libro.
        """
        if not nombre:
            return
        
        # Referencia al modelo de autor
        autor_model = self.env['biblioteca.autor']
        
        # Limpieza de datos
        nombre = nombre.strip()
        apellido = apellido.strip() if apellido else '.'
        
        # Buscar si ya existe
        # Nota: Usamos 'firstname' y 'lastname' porque así se llaman en tu modelo Autor
        autor_existente = autor_model.search([
            ('firstname', '=', nombre),
            ('lastname', '=', apellido)
        ], limit=1)
        
        if autor_existente:
            self.author = autor_existente.id
        else:
            # Si no existe, lo creamos
            nuevo_autor = autor_model.create({
                'firstname': nombre,
                'lastname': apellido
            })
            self.author = nuevo_autor.id

class Autor(models.Model):
    _name = 'biblioteca.autor'
    _description = 'Gestión de Autores'
    _rec_name_ = 'firstname'

    firstname = fields.Char(string='Nombre', required=True)
    lastname = fields.Char(string='Apellido', required=True)
    display_name = fields.Char(string='Nombre Completo', compute='_compute_display', store=True)

    @api.depends('firstname', 'lastname')
    def _compute_display(self):
        for record in self:
            record.display_name = f"{record.firstname} {record.lastname}"
   
          
class BibliotecaMulta(models.Model):
    _name = 'biblioteca.multa'
    _description = 'Gestión de Multas'

    prestamo_id = fields.Many2one('biblioteca.prestamo', string="Préstamo", required=True)
    usuario = fields.Many2one('biblioteca.usuario', string='Usuario', related='prestamo_id.usuario', store=True, readonly=True)
    monto = fields.Float(string='Valor a pagar')
    tipo_multa = fields.Selection(related='prestamo_id.tipo_multa', string='Tipo de multa', store=True, readonly=True)
    descripcion = fields.Char(string="Detalle multa")
    pago = fields.Selection(selection=[('pendiente','Pendiente'), ('saldada','Saldada')], string='Pago de la multa')

    motivo = fields.Selection(selection=[
        ('perdida','Pérdida'),
        ('retraso','Retraso'),
        ('daño','Daño'),
        ('robo', 'Robo'),
        ('otros','Otros')
    ], string='Causa de la multa')

    
class BibliotecaUsuario(models.Model):
    _name = 'biblioteca.usuario'
    _description = 'Gestión de Usuarios de la Biblioteca'
    _rec_name = 'nombre_completo'

    nombre = fields.Char(string='Nombre', required=True)
    apellido = fields.Char(string='Apellido', required=True)
    cedula = fields.Char(string='Cédula', required=True)
    telefono = fields.Char(string='Teléfono')
    correo = fields.Char(string='Correo electrónico')
    direccion = fields.Char(string='Dirección')
    nombre_completo = fields.Char(string='Nombre completo', compute='_compute_nombre_completo', store=True)
    fecha_vencimiento = fields.Date(String ='Estado de la membresía')
    estado_membresia = fields.Selection([
        ('activa', 'Activa'),
        ('vencida', 'Vencida'),
        ('inactiva', 'Suspendida')
        ], string = 'Estado de la Membresía', compute = '_compute_estado_membresia', store = True)
        
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'fecha_vencimiento' not in vals:
                fecha_actual = fields.Date.context_today(self)
                vals['fecha_vencimiento'] = fecha_actual + timedelta(days=6*30)
        return super().create(vals_list)

    
    @api.depends('fecha_vencimiento')
    def _compute_estado_membresia(self):
        hoy = fields.Date.today()
        for rec in self:
            if not rec.fecha_vencimiento:
                rec.estado_membresia = 'inactiva'
            elif rec.fecha_vencimiento >= hoy:
                rec.estado_membresia = 'activa'
            else:
                rec.estado_membresia = 'vencida'

    @api.depends('nombre', 'apellido')
    def _compute_nombre_completo(self):
        for record in self:
            record.nombre_completo = f"{record.nombre} {record.apellido}"

    @api.constrains('correo')
    def _check_correo(self):
        for rec in self:
            if rec.correo and '@' not in rec.correo:
                raise ValidationError('Ingrese un correo electrónico válido.')

    @api.constrains('cedula')
    def _check_cedula(self):
        for rec in self:
            # Puedes reutilizar el validador de cédula de tu clase CedulaEcuador
            valido, msg = CedulaEcuador._validar_cedula_ecuador(rec.cedula or '')
            if not valido:
                raise ValidationError(f'Cédula: {msg}')

class BibliotecaPrestamos(models.Model):
    _name = 'biblioteca.prestamo'
    _description = 'Modelo de manejo de prestamos'
    _rec_name = 'fecha_max'

    name = fields.Char(required=True)
    usuario = fields.Many2one('biblioteca.usuario', string='Usuario', required=True,
                              default=lambda self: self._default_usuario())
    fecha_prestamo = fields.Datetime(default=datetime.now(), string='Fecha de préstamo')
    libro = fields.Many2one('biblioteca.libro', string='Título de libro')
    fecha_devolucion = fields.Datetime()
    multa_bol = fields.Boolean(default=False)
    multa = fields.Float()
    estado = fields.Selection([
        ('b', 'Borrador'),
        ('p', 'Prestado'),
        ('m', 'Multa'),
        ('d', 'Devuelto')
    ], string='Estado', default='b')
    personal = fields.Many2one('res.users', string='Persona que prestó el libro',
                               default=lambda self: self.env.uid)
    fecha_max = fields.Datetime(compute='_compute_fecha_devo', string='Fecha Máxima de devolución')

    tipo_multa = fields.Selection(
        selection=[('perdida', 'Pérdida'),
                   ('retraso', 'Retraso'),
                   ('daño', 'Daño'),
                   ('robo', 'Robo'),
                   ('otros', 'Otros')],
        string='Tipo de multa')

    multa_otro_tipo = fields.Char(string='Especificar tipo de multa')

    @api.model
    def _default_usuario(self):
        usuario = self.env['biblioteca.usuario'].search([('correo', '=', self.env.user.email)], limit=1)
        return usuario.id if usuario else False

    @api.onchange('tipo_multa')
    def _onchange_tipo_multa(self):
        valores_tipos = {
            'perdida': 30.0,
            'retraso': 10.0,
            'daño': 25.0,
            'robo': 20.0,
            'otros': 0.0,
        }
        if self.tipo_multa in valores_tipos:
            self.multa = valores_tipos[self.tipo_multa]
        else:
            self.multa = 0.0
        if self.tipo_multa != 'otros':
            self.multa_otro_tipo = False

    def write(self, vals):
        if 'name' not in vals or not vals['name']:
            seq = self.env.ref('biblioteca.sequence_codigo_prestamos').next_by_code('biblioteca.prestamo')
            vals['name'] = seq
        return super(BibliotecaPrestamos, self).write(vals)

    @api.model
    def create(self, vals):
        if isinstance(vals, list):
            for val in vals:
                if not val.get('name'):
                    val['name'] = self.env.ref('biblioteca.sequence_codigo_prestamos').next_by_code('biblioteca.prestamo')
            return super(BibliotecaPrestamos, self).create(vals)
        else:
            if not vals.get('name'):
                vals['name'] = self.env.ref('biblioteca.sequence_codigo_prestamos').next_by_code('biblioteca.prestamo')
            return super(BibliotecaPrestamos, self).create(vals)

    def generar_prestamo(self):
        libro = self.libro
        if libro.ejemplares_disponibles <= 0:
            raise UserError(f"El libro '{libro.firstname}' no se encuentra en stock.")
        else:
            libro.ejemplares_disponibles -= 1
            libro.write({'ejemplares_disponibles': libro.ejemplares_disponibles})
            self.write({'estado': 'p'})  # marcar como prestado


    @api.depends('fecha_prestamo')
    def _compute_fecha_devo(self):
        for record in self:
            record.fecha_max = record.fecha_prestamo + timedelta(days=2)

    def _cron_multas(self):
        prestamos = self.env['biblioteca.prestamo'].search([
            ('estado', '=', 'p'),
            ('fecha_max', '<', datetime.now())
        ])
        for prestamo in prestamos:
            prestamo.write({'estado': 'm', 'multa_bol': True, 'multa': 1.0})
        prestamos_con_multa = self.env['biblioteca.prestamo'].search([('estado', '=', 'm')])
        
        for prestamo in prestamos_con_multa:
            days = (datetime.now() - prestamo.fecha_max).days
            prestamo.write({'multa': days})

    def asignar_multa(self):
        multa_model = self.env['biblioteca.multa']

        if not self.tipo_multa:
            raise UserError("Debe seleccionar el tipo de multa para asignar.")

        multas_existentes = multa_model.search([
            ('prestamo_id', '=', self.id),
            ('motivo', '=', self.tipo_multa)
        ])
        if multas_existentes:
            raise UserError("Ya existe una multa asignada con este motivo para este préstamo.")

        multa_model.create({
            'prestamo_id': self.id,
            'monto': self.multa,    
            'motivo': self.tipo_multa,
            'descripcion': self.multa_otro_tipo if self.tipo_multa == 'otros' else '',
            'pago': 'pendiente',
        })
        self.write({'estado': 'm', 'multa_bol': True})
        return True
    
    def devolver_libro(self):
        self.write({'estado': 'd', 'fecha_devolucion': fields.Datetime.now()})
        libro = self.libro
        if libro and libro.ejemplares_disponibles is not None:
            libro.ejemplares_disponibles += 1
            libro.write({'ejemplares_disponibles': libro.ejemplares_disponibles})



class CedulaEcuador(models.Model):
    _name = 'biblioteca.cedula'
    _description = 'Verificador de Cédula Ecuatoriana'
    _rec_name = 'cedula'

    cedula = fields.Char(string='Cédula', required=True)
    es_valida = fields.Boolean(string='Cédula válida', compute='_compute_validez', store=True)
    mensaje = fields.Char(string='Mensaje de validación', compute='_compute_validez', store=True)

    @api.depends('cedula')
    def _compute_validez(self):
        for rec in self:
            valido, msg = self._validar_cedula_ecuador(rec.cedula or '')
            rec.es_valida = valido
            rec.mensaje = msg

    @staticmethod
    def _validar_cedula_ecuador(cedula: str):
        cedula = (cedula or '').strip()
        if not cedula.isdigit():
            return False, "La cédula debe contener solo dígitos."
        if len(cedula) != 10:
            return False, "La cédula debe tener 10 dígitos."
        prov = int(cedula[:2])
        if prov < 1 or prov > 24:
            return False, "Código de provincia inválido."
        tercer = int(cedula[2])
        if tercer >= 6:
            return False, "Tercer dígito inválido para cédula natural."
        digitos = list(map(int, cedula))
        coef = [2, 1, 2, 1, 2, 1, 2, 1, 2]
        total = 0
        for i in range(9):
            prod = digitos[i] * coef[i]
            if prod >= 10:
                prod -= 9
            total += prod
        dig_verificador = (10 - (total % 10)) % 10
        if dig_verificador == digitos[9]:
            return True, "Cédula válida."
        return False, "Dígito verificador inválido."

    @api.constrains('cedula')
    def _check_cedula(self):
        for rec in self:
            valido, msg = self._validar_cedula_ecuador(rec.cedula or '')
            if not valido:
                raise ValidationError(msg)
